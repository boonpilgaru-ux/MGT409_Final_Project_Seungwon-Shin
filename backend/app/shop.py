"""Real t-shirt discovery via web search (SPEC section 5, shop.py).

routes.py calls ``await shop.suggest(session_id) -> list[Product]``.

Pipeline:
  1. Three web searches run CONCURRENTLY (Responses API + web_search, ``settings.shop_model``):
     local brands of the user's country, global brands, independent makers. Each must answer with
     a JSON array of individual product pages.
  2. Each search's answer is parsed leniently, cleaned (tracking params stripped), screened for
     obvious non-product URLs, and deduped against everything already claimed.
  3. Candidates are validated as soon as their search returns (pipelined with slower searches):
     SSRF-safe GET with manual redirects, status < 400, HTML, not sold out, not a soft 404, not a
     redirect to the home page. Page metadata fills price/currency.
  3b. Product photo: the first of og:image / twitter:image / itemprop image / JSON-LD Product
     images / the search-reported image that really displays on our page fills ``image_url``
     (``_usable_image``: fetched like the Shop's ``<img referrerpolicy="no-referrer">`` loads it,
     raster, >= MIN_IMAGE_PX, CORP allows cross-site, every hop on a public address).
  4. Survivors are interleaved across origins (per-store cap for variety), shirts with a photo
     first; shirts without one only fill in (see ``balance``), 8-12 returned.
Overall budget ``TOTAL_BUDGET_S``; whatever has been validated by then is returned. Once
MIN_RESULTS products are verified and two origins are done, the last origin gets SHOP_GRACE_S
more instead of the rest of the budget (plus LINK_CHECK_GRACE_S to check its links if its search
answers within it). A second call for the same session and profile while a search is running
joins it (page reloads don't double the cost).

Photos (measured 2026-10-26, 4 profiles): almost every shirt without a photo came from a
bot-walled retailer (Etsy, Musinsa, 29CM, Redbubble, TeePublic, Arket, COS, Levi's, Patagonia's
200 "Hang Tight!" page). An honest UA with fuller browser headers doesn't change their answer,
and the search models never report image URLs, so photos come from choosing shirts, not from
scraping harder: the prompts steer to brands' and makers' own web stores, ``balance`` picks
photographed shirts first, and a hedged search attempt that lost its race still contributes
candidates when photos are short.

Validation tiers (best first; ``balance`` ranks by tier within each origin):
  0  fetched, and the page carries product data (og:type product / JSON-LD Product / price meta)
  1  fetched, product-shaped URL, no structured product data
  2  UNVERIFIED: the retailer's bot protection (Cloudflare, DataDome, ...) answers every
     non-browser client with a 403/challenge, live page or not (e.g. Musinsa, 29CM, Etsy).
     Kept only for product-shaped URLs reported by the search; deliberate deviation from a strict
     "status < 400" rule, which would otherwise drop the main local and indie retailers outright.
Hard failures (404/410, other 4xx/5xx, timeouts, soft 404s, redirects to the home page or a
listing page) are dropped.

Searches use low reasoning effort and ask for ``CANDIDATES_PER_SEARCH`` items to stay under the
gateway's ~40 s request timeout (observed: 408 "The operation was timeout"), and are hedged: if a
search hasn't produced candidates after ``HEDGE_AFTER_S`` (or failed / came back empty), a second
independent request starts on ``settings.shop_backup_model`` and the first success wins (the
other keeps running in ``run.late``, see ``_search``). Every prompt carries a random tag because
the gateway caches identical requests, which would otherwise make "Refresh suggestions" replay the
previous answer.

SSRF safety (``_check_url`` + ``_GuardedBackend``): http/https on default ports only, no
credentials, no local hostnames, every resolved address must be public; checked before each hop
AND at socket-connect time (connecting to the vetted IP itself defeats DNS rebinding).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import secrets
import socket
import struct
import time
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import partial
from decimal import Decimal, InvalidOperation
from itertools import zip_longest
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpcore
import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from . import context, llm, store
from .config import PUBLIC_URL, get_settings
from .errors import ConflictError, LLMError, UpstreamError
from .models import Country, Origin, Product, Profile

__all__ = ["suggest", "ORIGINS"]

log = logging.getLogger("teedentity.shop")

ORIGINS: tuple[Origin, ...] = ("local", "global", "indie")

TOTAL_BUDGET_S = 75.0  # whole suggest() call
SHOP_GRACE_S = 6.0  # with MIN_RESULTS verified and 2 of 3 origins done, the last origin gets this long
LINK_CHECK_GRACE_S = 4.0  # ...plus this long to check its links if its search answered within it
SEARCH_TIMEOUT_S = 58.0  # one web search (HTTP timeout; asyncio adds a few seconds of slack)
VALIDATION_RESERVE_S = 12.0  # time kept back for validating after the slowest search
FETCH_TIMEOUT_S = 9.0  # one product page, all redirect hops included
MAX_REDIRECTS = 5
MAX_PAGE_BYTES = 1_200_000
VALIDATION_CONCURRENCY = 10
CANDIDATES_PER_SEARCH = 6  # more makes each search slower than the gateway's ~40 s timeout
MIN_RESULTS, MAX_RESULTS = 8, 12
PER_DOMAIN_CAP = 2
SEARCH_REASONING_EFFORT = "low"
SEARCH_CONTEXT_SIZE = "low"  # less page context per search hit: noticeably faster answers
HEDGE_AFTER_S = 12.0  # start a backup search (on settings.shop_backup_model) if the first is this slow
# Product photos: each candidate image is fetched (first bytes only) before it is shown.
IMAGE_TIMEOUT_S = 4.0  # one image request
IMAGE_BUDGET_S = 6.0  # all photo candidates of one product together
MAX_IMAGE_TRIES = 3  # photo candidates tried per product (og:image, JSON-LD image, search-reported...)
MAX_IMAGE_REDIRECTS = 3
IMAGE_SNIFF_BYTES = 64 * 1024  # enough for the dimensions of PNG/GIF/WebP/JPEG headers
MIN_IMAGE_PX = 100  # smaller is a spacer, tracking pixel or icon (the frontend drops < 40 px too)
MAX_PHOTOLESS_SHARE = 0.25  # with enough photographed shirts, at most this share may lack a photo

# Honest crawler-style UA (no personal data). Bot-walled retailers (Musinsa, Etsy, Arket, ...)
# answer this UA and the bare "Teedentity/0.1" alike with a 403 (measured 2026-10-26); we never
# impersonate a browser to get past them.
USER_AGENT = f"Mozilla/5.0 (compatible; Teedentity/0.1; +{PUBLIC_URL}; student project link checker)"
_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.8,*;q=0.5",
}
_IMAGE_HEADERS = {"Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.5"}
_NON_PHOTO_TYPES = {"image/svg+xml", "image/x-icon", "image/vnd.microsoft.icon"}  # logos and icons

# =========================================================================== SSRF-safe fetching


class BlockedURL(ValueError):
    """The URL points somewhere we must not fetch (bad scheme/port, private address, ...)."""


_ALLOWED_PORTS = {None, 80, 443}
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
        or ip.is_unspecified
    )


async def _resolve_ips(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """All addresses ``host`` resolves to (IP literals pass straight through). Tests monkeypatch this."""
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    infos = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=4.0
    )
    ips = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
    return list(dict.fromkeys(ips))


async def _public_ips(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve and require that EVERY address is public (a mixed answer is treated as hostile)."""
    lowered = host.lower().rstrip(".")
    if not lowered or lowered == "localhost" or lowered.endswith(_LOCAL_SUFFIXES):
        raise BlockedURL(f"local hostname {host!r}")
    try:
        ips = await _resolve_ips(lowered, port)
    except (OSError, asyncio.TimeoutError) as err:
        raise BlockedURL(f"cannot resolve {host!r}") from err
    if not ips or not all(_is_public_ip(ip) for ip in ips):
        raise BlockedURL(f"{host!r} resolves to a non-public address")
    return ips


async def _check_url(url: str) -> None:
    """Raise BlockedURL unless ``url`` is a fetchable public http(s) URL."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as err:
        raise BlockedURL("malformed URL") from err
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedURL("only http(s) URLs are allowed")
    if parts.username or parts.password:
        raise BlockedURL("credentials in URL")
    if port not in _ALLOWED_PORTS:
        raise BlockedURL(f"port {port} not allowed")
    await _public_ips(parts.hostname, port or (443 if parts.scheme == "https" else 80))


class _GuardedBackend(httpcore.AsyncNetworkBackend):
    """Network backend that re-vets the address at connect time and connects to that exact IP.

    TLS still verifies the certificate against the original hostname (httpcore passes the URL host
    as ``server_hostname``), so pinning the IP is transparent to HTTPS.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend) -> None:
        self._inner = inner

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            ips = await _public_ips(host, port)
        except BlockedURL as err:
            raise httpcore.ConnectError(f"blocked: {err}") from err
        last: Exception | None = None
        for ip in ips[:3]:
            try:
                return await self._inner.connect_tcp(
                    str(ip), port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as err:
                last = err
        raise last or httpcore.ConnectError("no address to connect to")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _make_transport() -> httpx.AsyncBaseTransport:
    """Real transport with the connect-time guard installed. Tests monkeypatch this.

    Uses context.py's cached SSL context: building a fresh one loads the CA bundle (~0.2 s) on the
    event loop, which stalled every other request each time a shop search started.
    """
    transport = httpx.AsyncHTTPTransport(retries=0, verify=context._ssl_context())
    pool = getattr(transport, "_pool", None)
    inner = getattr(pool, "_network_backend", None)
    if isinstance(inner, httpcore.AsyncNetworkBackend):
        pool._network_backend = _GuardedBackend(inner)  # type: ignore[union-attr]
    else:  # pragma: no cover - httpcore internals changed; per-hop _check_url still applies
        log.warning("shop: connect-time SSRF guard unavailable; relying on per-hop checks")
    return transport


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_make_transport(),
        headers=_HEADERS,
        timeout=httpx.Timeout(FETCH_TIMEOUT_S, connect=4.0),
        follow_redirects=False,
        trust_env=False,  # never route through env proxies (would bypass the connect-time guard)
        limits=httpx.Limits(max_connections=VALIDATION_CONCURRENCY * 2, max_keepalive_connections=10),
    )


@dataclass
class _Page:
    url: str  # final URL after redirects
    html: str
    redirected_home: bool = False
    walled: bool = False  # bot protection answered instead of the retailer (status >= 400)


_WALL_STATUSES = {401, 403, 429, 503}
_WALL_SERVERS = ("cloudflare", "datadome", "akamaighost", "ddos-guard", "sucuri")
_WALL_HEADERS = ("x-datadome", "cf-mitigated", "x-iinfo", "x-amzn-waf-action", "x-sucuri-id", "x-px-block")


def _is_bot_wall(resp: httpx.Response) -> bool:
    """A bot-protection refusal (Cloudflare, DataDome, Akamai, Imperva, AWS WAF...) rather than a
    real "not found": such sites refuse every non-browser client whether or not the page exists."""
    if resp.status_code not in _WALL_STATUSES:
        return False
    server = resp.headers.get("server", "").lower()
    return (
        resp.status_code == 429
        or any(name in server for name in _WALL_SERVERS)
        or any(header in resp.headers for header in _WALL_HEADERS)
    )


_REDIRECT_CODES = {301, 302, 303, 307, 308}
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.IGNORECASE)


def _decode(body: bytes, response: httpx.Response) -> str:
    charset = response.charset_encoding
    if not charset:
        match = _META_CHARSET_RE.search(body[:4096])
        charset = match.group(1).decode("ascii", "ignore") if match else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


async def _fetch_page(http: httpx.AsyncClient, url: str) -> _Page | None:
    """GET an HTML page safely, following up to MAX_REDIRECTS (each hop re-vetted).

    Returns None for blocked/dead/non-HTML URLs, a ``walled`` page (no HTML) when bot protection
    refused us, else the page. Network errors propagate as httpx exceptions.
    """
    original_path = urlsplit(url).path.strip("/")
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            await _check_url(current)
        except BlockedURL as err:
            log.info("shop: blocked %s (%s)", _short(current), err)
            return None
        async with http.stream("GET", current) as resp:
            if resp.status_code in _REDIRECT_CODES:
                location = resp.headers.get("location")
                if not location:
                    return None
                current = urljoin(current, location.strip())
                continue
            if resp.status_code >= 400:
                walled = _is_bot_wall(resp)
                log.info("shop: %s -> HTTP %d%s", _short(current), resp.status_code, " (bot wall)" if walled else "")
                return _Page(url=current, html="", walled=True) if walled else None
            ctype = resp.headers.get("content-type", "").lower()
            if ctype and "html" not in ctype:
                return None
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= MAX_PAGE_BYTES:
                    break
            html = _decode(b"".join(chunks)[:MAX_PAGE_BYTES], resp)
        return _Page(url=current, html=html, redirected_home=bool(original_path) and _is_home(current))
    log.info("shop: too many redirects for %s", _short(url))
    return None


_LOCALE_SEGMENT_RE = re.compile(r"[a-z]{2}(?:[-_][a-z]{2})?")


def _is_home(url: str) -> bool:
    """'/', '/kr/', '/en-us', '/kr/ko' - where retailers bounce dead product links."""
    segments = [s.lower() for s in urlsplit(url).path.split("/") if s]
    return len(segments) <= 2 and all(_LOCALE_SEGMENT_RE.fullmatch(s) for s in segments)


def _short(url: str) -> str:
    return url if len(url) <= 120 else url[:117] + "..."


# =========================================================================== page metadata

# JSON-LD blocks are found with literal scans, not one DOTALL lazy regex over the whole page: that
# regex is quadratic on unclosed <script> tags (a hostile 1.2 MB page froze the server for minutes,
# since the regex engine holds the GIL). Every step below is a linear literal search.
_LD_JSON_RE = re.compile(r"application/ld\+json", re.IGNORECASE)
_SCRIPT_OPEN_RE = re.compile(r"<script\b", re.IGNORECASE)
_SCRIPT_CLOSE_RE = re.compile(r"</script\s*>", re.IGNORECASE)
JSONLD_MAX_BLOCKS = 30
_JSONLD_TAG_REACH = 300  # max chars between "<script" and its type="application/ld+json"
_HEAD_END_RE = re.compile(r"</head\s*>", re.IGNORECASE)
_STRAY_TITLE_RE = re.compile(r"<title\b[^>]{0,200}>([^<]{1,300})", re.IGNORECASE)  # bounded: linear
_CHALLENGE_TITLES = (
    "just a moment", "attention required", "access denied", "are you a robot", "robot check",
    "security check", "pardon our interruption", "verify you are human", "captcha", "request blocked",
    "403 forbidden", "hang tight",  # Akamai's 200 "Hang Tight! Routing to checkout..." failover page
)
# Product photo candidates in <head>, best first ("image" = itemprop="image").
_IMAGE_META_KEYS = (
    "og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src", "image",
)
_MISSING_TITLES = ("page not found", "404", "not found", "no longer available", "product unavailable")
_SOLD_OUT_MARKERS = ("outofstock", "soldout", "discontinued")


@dataclass
class _PageInfo:
    title: str | None = None
    description: str | None = None
    site_name: str | None = None
    images: list[str] = field(default_factory=list)  # product photo candidates, best first (may be relative)
    price: str | None = None
    currency: str | None = None
    brand: str | None = None
    is_product: bool = False
    sold_out: bool = False
    walled: bool = False  # a bot challenge page was served instead of the product
    missing: bool = False  # soft 404 ("page not found" with status 200)


def _jsonld_blocks(html: str) -> Iterable[str]:
    """Raw text of up to JSONLD_MAX_BLOCKS ld+json scripts, in page order (linear time)."""
    pos = 0
    for _ in range(JSONLD_MAX_BLOCKS):
        hit = _LD_JSON_RE.search(html, pos)
        if hit is None:
            return
        pos = hit.end()
        window_start = max(0, hit.start() - _JSONLD_TAG_REACH)
        opens = [m.start() for m in _SCRIPT_OPEN_RE.finditer(html, window_start, hit.start())]
        tag_end = html.find(">", hit.end())
        if not opens or tag_end < 0 or ">" in html[opens[-1] : hit.start()]:
            continue  # the words appear outside a <script ...> tag
        closing = _SCRIPT_CLOSE_RE.search(html, tag_end + 1)
        if closing is None:
            return  # unclosed: no later block can be closed either
        yield html[tag_end + 1 : closing.start()]
        pos = closing.end()


def _iter_jsonld(html: str) -> Iterable[dict]:
    """Every JSON-LD object on the page (flattening lists and @graph)."""
    for block in _jsonld_blocks(html):
        try:
            data = json.loads(block.strip(), strict=False)
        except (json.JSONDecodeError, ValueError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                if isinstance(node.get("@graph"), list):
                    stack.extend(node["@graph"])


def _first_str(value: Any) -> str | None:
    """First usable string from str | list | {url|name: ...}."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for item in value:
            found = _first_str(item)
            if found:
                return found
    if isinstance(value, dict):
        return _first_str(value.get("url") or value.get("contentUrl") or value.get("name"))
    return None


def _strs(value: Any, limit: int) -> list[str]:
    """Up to ``limit`` strings from str | [str | {url|contentUrl: ...}] (JSON-LD image lists)."""
    items = value if isinstance(value, list) else [value]
    found = [_first_str(item) for item in items[: limit * 2]]
    return [s for s in found if s][:limit]


def _is_type(node: dict, *names: str) -> bool:
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and t in names for t in types)


def _offer_fields(offers: Any) -> tuple[str | None, str | None, list[str]]:
    """(price, currency, availabilities) from a JSON-LD offers value."""
    nodes = offers if isinstance(offers, list) else [offers]
    price = currency = None
    availability: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("offers"), (list, dict)):  # AggregateOffer nesting
            p, c, a = _offer_fields(node["offers"])
            price, currency = price or p, currency or c
            availability += a
        raw_price = node.get("price", node.get("lowPrice"))
        if price is None and raw_price not in (None, ""):
            price = str(raw_price)
            currency = _first_str(node.get("priceCurrency")) or currency
        if isinstance(node.get("availability"), str):
            availability.append(node["availability"])
    return price, currency, availability


def _page_info(page: _Page) -> _PageInfo:
    info = _PageInfo()
    head_end = _HEAD_END_RE.search(page.html)
    head = page.html[: head_end.start()] if head_end else page.html[:300_000]
    soup = BeautifulSoup(head, "html.parser")
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").strip().lower()
        content = (tag.get("content") or "").strip()
        if key and content and key not in meta:
            meta[key] = content
    title_tag = soup.find("title")
    page_title = title_tag.get_text(" ", strip=True) if title_tag else None

    info.title = meta.get("og:title") or meta.get("twitter:title") or page_title
    info.description = meta.get("og:description") or meta.get("description") or meta.get("twitter:description")
    info.site_name = meta.get("og:site_name")
    images = [meta.get(key) for key in _IMAGE_META_KEYS]
    link = soup.find("link", rel=lambda v: v and "image_src" in v)
    images.append(link.get("href") if link else None)
    info.price = meta.get("product:price:amount") or meta.get("og:price:amount")
    info.currency = meta.get("product:price:currency") or meta.get("og:price:currency")
    availability = [meta.get("product:availability") or meta.get("og:availability") or ""]
    info.is_product = "product" in meta.get("og:type", "").lower() or bool(info.price)

    for node in _iter_jsonld(page.html):
        if not _is_type(node, "Product", "ProductGroup", "IndividualProduct"):
            continue
        info.is_product = True
        info.title = info.title or _first_str(node.get("name"))
        info.description = info.description or _first_str(node.get("description"))
        images += _strs(node.get("image"), 2)
        info.brand = info.brand or _first_str(node.get("brand"))
        price, currency, avail = _offer_fields(node.get("offers"))
        if info.price is None and price:
            info.price, info.currency = price, currency or info.currency
        availability += avail
        if isinstance(node.get("hasVariant"), list):  # ProductGroup: availability lives on variants
            for variant in node["hasVariant"]:
                if isinstance(variant, dict):
                    availability += _offer_fields(variant.get("offers"))[2]
        break
    info.images = list(dict.fromkeys(i.strip() for i in images if isinstance(i, str) and i.strip()))

    # "https://schema.org/OutOfStock" / "out of stock" / "oos" -> "outofstock" / "oos"; sold out only
    # if EVERY reported availability says so (one in-stock variant is enough).
    flags = [re.sub(r"[^a-z]", "", a.lower().rsplit("/", 1)[-1]) for a in availability if a]
    info.sold_out = bool(flags) and all(f == "oos" or any(m in f for m in _SOLD_OUT_MARKERS) for f in flags)
    if not info.is_product and page_title:
        lowered = page_title.lower()
        info.walled = any(m in lowered for m in _CHALLENGE_TITLES)
        info.missing = not info.walled and any(m in lowered for m in _MISSING_TITLES)
    elif not info.is_product and not info.title:
        # Some challenge pages close <head> early and put their <title> in the body (Akamai's
        # "Hang Tight!" page). Only used to spot those: a body <title> may be an SVG icon's.
        stray = _STRAY_TITLE_RE.search(page.html, 0, 300_000)
        info.walled = bool(stray) and any(m in stray.group(1).lower() for m in _CHALLENGE_TITLES)
    return info


# =========================================================================== candidates from the model


def _coerce_text(value: Any) -> Any:
    """Numbers -> str; {"name": ...} / [..] -> first string inside (models aren't always tidy)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (dict, list)):
        return _first_str(value)
    return value


class RawProduct(BaseModel):
    """One product as reported by the search model (lenient; validated later against the page)."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    url: str
    title: str = ""
    brand: str = ""
    retailer: str = ""
    image_url: str | None = None
    price: str | None = None
    currency: str | None = None
    why: str = ""

    @field_validator("title", "brand", "retailer", "why", "price", "currency", "image_url", mode="before")
    @classmethod
    def _to_text(cls, value: Any) -> Any:
        return _coerce_text(value)


_KEY_ALIASES = {
    "url": ("url", "product_url", "productUrl", "link", "href", "page_url"),
    "title": ("title", "name", "product_name", "productName", "product"),
    "brand": ("brand", "brand_name", "maker", "designer", "artist"),
    "retailer": ("retailer", "store", "shop", "site", "seller", "marketplace"),
    "image_url": ("image_url", "imageUrl", "image", "img", "thumbnail", "photo"),
    "price": ("price", "price_text", "cost"),
    "currency": ("currency", "price_currency", "priceCurrency"),
    "why": ("why", "reason", "rationale", "fit", "why_its_you", "match"),
}
_LIST_KEYS = ("products", "items", "results", "shirts", "tshirts", "t_shirts", "data")


def _normalize_item(item: dict) -> dict:
    out: dict[str, Any] = {}
    for field_name, aliases in _KEY_ALIASES.items():
        for alias in aliases:
            if item.get(alias) not in (None, ""):
                out[field_name] = item[alias]
                break
    return out


def parse_candidates(text: str) -> list[RawProduct]:
    """Lenient parse of a search answer into RawProducts; bad items are skipped, not fatal."""
    try:
        data = llm.extract_json(text)
    except ValueError:
        return []
    if isinstance(data, dict):
        inner = next((data[k] for k in _LIST_KEYS if isinstance(data.get(k), list)), None)
        data = inner if inner is not None else [data]
    if not isinstance(data, list):
        return []
    products: list[RawProduct] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            products.append(RawProduct.model_validate(_normalize_item(item)))
        except ValidationError:
            continue
    return products


# =========================================================================== URL hygiene

_MD_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
_TRACKING_PARAMS = {
    "gclid", "fbclid", "msclkid", "srsltid", "mc_cid", "mc_eid", "_pos", "_sid", "_ss", "_psq", "_ga",
    "ref", "ref_", "ref_src", "spm", "scm",
}
_NON_PRODUCT_HOSTS = (
    "google.", "bing.com", "duckduckgo.com", "yahoo.", "pinterest.", "instagram.com", "facebook.com",
    "tiktok.com", "twitter.com", "x.com", "reddit.com", "youtube.com", "youtu.be", "wikipedia.org",
    "medium.com", "threads.net", "tumblr.com", "linktr.ee",
)
_SEARCH_QUERY_KEYS = {"q", "query", "search", "keyword", "keywords", "searchterm", "search_query", "k", "s", "term"}
_LISTING_SEGMENTS = {
    "search", "searches", "collections", "collection", "category", "categories", "catalog", "browse",
    "tag", "tags", "brands", "brand", "shop", "store", "stores", "sale", "new-arrivals", "all", "men",
    "women", "mens", "womens", "t-shirts", "tshirts", "tees", "tops", "products", "c", "market",
}
_EDITORIAL_SEGMENTS = {
    "blog", "blogs", "news", "magazine", "journal", "article", "articles", "stories", "guide",
    "pages",  # Shopify CMS pages (/pages/sale-promo-codes?variant=... looked product-shaped)
}
_PRODUCT_SEGMENTS = {"products", "product", "p", "dp", "listing", "item", "items", "goods", "gp", "pd", "prd"}
_LONG_ID_RE = re.compile(r"\d{5,}")
_SECOND_LEVEL = {"co", "com", "net", "org", "ac", "go", "or", "ne", "gov", "edu", "ltd", "plc"}


def clean_url(raw: str) -> str | None:
    """Normalize a model-reported URL: unwrap markdown, require http(s), drop tracking params + fragment."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    md = _MD_LINK_RE.search(text)
    if md:
        text = md.group(1)
    text = text.strip("<>()[]'\" ").rstrip(".,;")
    if text.startswith("//"):
        text = "https:" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or " " in text:
        return None
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), ""))


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def site_of(url: str) -> str:
    """Approximate registrable domain: shop.example.co.kr -> example.co.kr, a.b.com -> b.com."""
    labels = _host(url).split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# Platforms that give every maker their own subdomain (artist.bandcamp.com, studio.bigcartel.com):
# for the per-store cap each subdomain is a separate shop.
_MAKER_SUBDOMAIN_PLATFORMS = (
    "bandcamp.com", "bigcartel.com", "storenvy.com", "cargo.site", "myshopify.com", "fourthwall.com",
    "creator-spring.com",
)


def store_of(url: str) -> str:
    """The shop a product belongs to: its site, or the maker's subdomain on a maker platform."""
    host = _host(url)
    if any(host.endswith("." + platform) for platform in _MAKER_SUBDOMAIN_PLATFORMS):
        return host
    return site_of(url)


def url_key(url: str) -> str:
    parts = urlsplit(url)
    query = "&".join(sorted(parts.query.split("&"))) if parts.query else ""
    return f"{_host(url)}{parts.path.rstrip('/').lower()}?{query}"


def _title_key(url: str, title: str) -> str:
    return f"{site_of(url)}|{re.sub(r'[^0-9a-z가-힣ぁ-んァ-ン一-龥]+', '', title.lower())[:60]}"


def _host_matches(host: str, pattern: str) -> bool:
    """'x.com' matches x.com and *.x.com; 'google.' matches google.<any tld> and its subdomains."""
    if pattern.endswith("."):
        return f".{pattern}" in f".{host}"
    return host == pattern or host.endswith("." + pattern)


def has_product_marker(url: str) -> bool:
    """URL shape typical of a single product: /products/<slug>, /dp/<id>, /listing/<id>, long ids..."""
    parts = urlsplit(url)
    segments = [s.lower() for s in parts.path.split("/") if s]
    if any(s in _PRODUCT_SEGMENTS for s in segments[:-1]) or (segments and segments[0] in _PRODUCT_SEGMENTS):
        return len(segments) >= 2 or bool(parts.query)
    return bool(_LONG_ID_RE.search(parts.path)) or any(k.lower() in ("id", "pid", "product_id", "goodsno", "variant")
                                                      for k, _ in parse_qsl(parts.query))


def is_non_product_url(url: str) -> bool:
    """Cheap pre-fetch screen for pages that can't be a single product: search, category,
    collection, home, editorial, social. Errs on the side of keeping the URL."""
    parts = urlsplit(url)
    host = _host(url)
    if any(_host_matches(host, pattern) for pattern in _NON_PRODUCT_HOSTS):
        return True
    segments = [s.lower() for s in parts.path.split("/") if s]
    if not segments:
        return True
    query_keys = {k.lower() for k, _ in parse_qsl(parts.query)}
    if query_keys & _SEARCH_QUERY_KEYS or "search" in segments or segments == ["s"]:
        return True
    if any(s in _EDITORIAL_SEGMENTS for s in segments):
        return True
    if has_product_marker(url):
        return False
    if "collections" in segments or segments[0] in ("c", "category", "categories", "browse", "tag", "tags", "market"):
        return True
    return segments[-1] in _LISTING_SEGMENTS


# =========================================================================== prices

_CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "KRW": "₩", "CNY": "¥", "INR": "₹", "AUD": "A$",
    "CAD": "CA$", "NZD": "NZ$", "HKD": "HK$", "SGD": "S$", "TWD": "NT$", "BRL": "R$", "MXN": "MX$",
    "THB": "฿", "VND": "₫", "PHP": "₱", "ILS": "₪", "TRY": "₺", "PLN": "zł", "CHF": "CHF ",
}
_ZERO_DECIMAL = {"JPY", "KRW", "VND", "TWD", "CLP", "ISK", "HUF", "IDR"}
_SYMBOL_TO_CODE = (
    ("₩", "KRW"), ("€", "EUR"), ("£", "GBP"), ("₹", "INR"), ("A$", "AUD"), ("CA$", "CAD"), ("C$", "CAD"),
    ("NZ$", "NZD"), ("HK$", "HKD"), ("S$", "SGD"), ("NT$", "TWD"), ("R$", "BRL"), ("₫", "VND"),
    ("฿", "THB"), ("₱", "PHP"), ("円", "JPY"), ("원", "KRW"), ("US$", "USD"), ("$", "USD"),
)


def format_price(amount: str, currency: str | None) -> str | None:
    """'39000', 'KRW' -> '₩39,000'; '28.5', 'USD' -> '$28.50'; unknown currency -> '12.00 XYZ'."""
    try:
        value = Decimal(str(amount).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if value <= 0:
        return None
    code = (currency or "").upper().strip()
    number = f"{value:,.0f}" if code in _ZERO_DECIMAL else f"{value:,.2f}"
    if code in _CURRENCY_SYMBOLS:
        return f"{_CURRENCY_SYMBOLS[code]}{number}"
    return f"{number} {code}".strip()


def _currency_code(currency: str | None, price: str | None) -> str | None:
    if currency and re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
        return currency.strip().upper()
    for symbol, code in _SYMBOL_TO_CODE:
        if price and symbol in price:
            return code
    return None


# =========================================================================== search prompts

SEARCH_INSTRUCTIONS = (
    "You are a sharp personal shopper who finds real t-shirts that are for sale online right now. "
    "Use web search. Only report URLs that appeared in your search results in this session; never guess, "
    "shorten, or construct a URL. Every URL must be the product detail page of ONE specific t-shirt "
    "(not a search, category, collection, brand home, lookbook, blog, review, or social media page). "
    "Skip items that are sold out. Reply with a JSON array only."
)

_FOCUS = {
    "local": (
        "local brands",
        "t-shirts from brands founded and based in {country} (local streetwear, design and heritage "
        "labels), mostly from the brands' own online stores (at most 2 from multi-brand {country} "
        "fashion retailers or marketplaces). You may search in the local language; write title and "
        "why in English.",
    ),
    "global": (
        "global brands",
        "t-shirts from internationally known brands or official merch stores (for example Uniqlo UT collabs, "
        "official anime/game/band/team stores, Muji, Carhartt WIP, Stussy, "
        "Champion, Nike, A.P.C., Norse Projects, Obey, Brain Dead) on the brand's official store or a "
        "major international retailer, preferably available in {country}. No Etsy, Redbubble or other "
        "marketplace / print-on-demand sellers: established brands only.",
    ),
    "indie": (
        "independent makers",
        "t-shirts from independent makers themed on their picks: illustrators' and small studios' own web shops (Shopify, "
        "Big Cartel, Cargo), Bandcamp artist merch, small-batch print studios, at most 2 Etsy "
        "listings; no print-on-demand marketplaces (Redbubble, TeePublic, Threadless, Society6, "
        "Zazzle, Spreadshirt); ideally some from makers in {country}.",
    ),
}


# Marketplaces whose sellers are independent makers: a "global" hit there is really "indie".
_INDIE_MARKETPLACES = (
    "etsy.com", "bandcamp.com", "bigcartel.com", "redbubble.com", "society6.com", "threadless.com",
    "teepublic.com", "storenvy.com", "cargo.site", "inprnt.com", "spreadshirt.", "teespring.com",
    "creator-spring.com", "idus.com", "marpple.com", "zazzle.", "fourthwall.com",
)


def origin_for(url: str, searched: Origin) -> Origin:
    """Origin label for a result: the search's focus, except marketplace-of-makers hits from the
    global search, which are relabelled "indie" (local makers stay "local")."""
    if searched == "global" and any(_host_matches(_host(url), m) for m in _INDIE_MARKETPLACES):
        return "indie"
    return searched



def picks_from_answers(answers) -> tuple[str, ...]:
    """'Chosen (local name) over rejected' for every balance-game answer: the strongest signal of what
    this person would actually wear (a fandom, food, team, place or style they picked)."""
    picks = []
    for a in answers:
        q = a.question
        chosen, other = (q.option_a, q.option_b) if a.choice == "A" else (q.option_b, q.option_a)
        name = f"{chosen.label} ({chosen.sublabel})" if chosen.sublabel else chosen.label
        picks.append(f"{name} over {other.label}")
    return tuple(picks)


def _picks_block(picks: tuple[str, ...]) -> str:
    if not picks:
        return ""
    lines = "\n".join(f"  {i}. {p}" for i, p in enumerate(picks, 1))
    return f"""
THEIR BALANCE-GAME PICKS (what they chose over what) - THE MOST IMPORTANT INPUT:
{lines}

Every t-shirt MUST visibly feature or directly reference one specific thing they PICKED above (never the
thing they rejected): official or licensed merch of a picked anime, game, band, idol group, show or team
(e.g. a Uniqlo UT collab, an official team jersey tee), a graphic tee of a picked food, snack, place or
mascot, or a tee from a picked brand, music scene or era. Spread the shirts across at least 4 different
picks with at most 2 shirts per pick, favoring the most wearable ones (fandoms, foods, teams, music, places,
brands). Official or licensed merch first, then well-made graphic tees from real shops. Use the profile
below only to choose between candidates (style, colors, fit). "why" MUST start with "You picked <the
thing>" and say how the shirt shows it.
"""

_RELAXED_NOTE = (
    "\nThis is a second attempt: the taste match may be looser. Prioritize real, in-stock product pages "
    "that fit the search focus; any well-made t-shirt that still features one of their picks is fine.\n"
)


def _search_prompt(
    origin: Origin, profile: Profile, country: Country, relaxed: bool = False, picks: tuple[str, ...] = ()
) -> str:
    """Prompt for one search. ``relaxed`` (used by the backup request) loosens the taste match so the
    retry isn't a carbon copy of an attempt that came back empty."""
    title, focus = _FOCUS[origin]
    taste = profile.taste
    colors = ", ".join(c.name for c in taste.colors)
    return f"""Search focus: {title}.
Find {CANDIDATES_PER_SEARCH} {focus.format(country=country.name)}
{_picks_block(picks)}
They are for this person (country: {country.name}):
- Archetype: {profile.archetype} - {profile.tagline}
- T-shirt brief: {profile.tshirt_brief}
- Aesthetics: {", ".join(taste.aesthetics)}
- Motifs: {", ".join(taste.motifs)}
- Favorite colors: {colors}
- Typography: {taste.typography}
- Fits: {", ".join(taste.fits)}
- Avoid: {", ".join(taste.avoid) or "nothing specific"}

The motifs are the actual things they picked in the game (fandoms, foods, places, eras). Strongly prefer
t-shirts that literally feature one of them: official or licensed merch (e.g. an official Slam Dunk tee),
a graphic tee of that food or place, or a tee from that music scene or era. Use the brief for style.

Rules: real t-shirts only (no hoodies, sweatshirts, collared shirts or accessories); at most 2 from the
same website or marketplace (all of Etsy counts as one), different brands where possible; styles that
genuinely match this person and avoid what they dislike; currently in stock. Prefer each brand's or
maker's own web store: big marketplaces often block the link check that lets us show the shirt's photo.
If exact matches are scarce, include the closest good options instead of returning fewer or none.
{_RELAXED_NOTE if relaxed else ""}
Return ONLY a JSON array (no prose, no code fences) of objects with these keys:
"title" (product name), "brand", "retailer" (store name), "url" (exact product page URL from your search),
"image_url" (direct product image URL if you saw one, else null), "price" (as displayed, e.g. "₩39,000" or
"$28.00", else null), "currency" (ISO 4217 code or null), "why" (one sentence, max 22 words, addressed to
the person as "you", tying the shirt to their specific taste).

(Search run {secrets.token_hex(4)}: the gateway caches identical requests, so this code keeps every
"Refresh suggestions" a genuinely new search. It has no other meaning.)"""


# =========================================================================== orchestration


TIER_PRODUCT_PAGE, TIER_PRODUCT_URL, TIER_UNVERIFIED = 0, 1, 2  # see module docstring


@dataclass
class _Found:
    product: Product
    tier: int


@dataclass
class _Run:
    http: httpx.AsyncClient
    deadline: float
    semaphore: asyncio.Semaphore
    claimed: set[str] = field(default_factory=set)
    titles: set[str] = field(default_factory=set)
    final_urls: set[str] = field(default_factory=set)
    found: dict[str, list[_Found]] = field(default_factory=lambda: {o: [] for o in ORIGINS})
    stats: Counter = field(default_factory=Counter)
    late: set[asyncio.Task[None]] = field(default_factory=set)  # hedged attempts that lost their race
    answered: set[str] = field(default_factory=set)  # origins whose search answered (validating now)
    picks: tuple[str, ...] = ()  # balance-game picks every shirt must reference

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def photographed(self) -> int:
        return sum(1 for found in self.found.values() for f in found if f.product.image_url)


def _absolute_image(src: str | None, base: str) -> str | None:
    if not src:
        return None
    src = src.strip()
    if src.startswith("data:"):
        return None
    full = urljoin(base, src)  # resolves relative and protocol-relative (//cdn...) URLs
    parts = urlsplit(full)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return full


async def _validate(run: _Run, raw: RawProduct, url: str, origin: Origin) -> _Found | None:
    """Fetch + verify one candidate and build the Product, or None if it doesn't hold up."""
    page = await _fetch_page(run.http, url)
    if page is None:
        run.stats["dead"] += 1
        return None
    if page.redirected_home:
        run.stats["home"] += 1
        return None
    final_url = clean_url(page.url) or url
    if url_key(final_url) != url_key(url) and is_non_product_url(final_url):
        run.stats["home"] += 1  # a gone product bounced to a listing (seen: END. -> /collections/<brand>)
        return None
    info = _PageInfo(walled=True) if page.walled else await asyncio.to_thread(_page_info, page)
    if info.missing:
        run.stats["dead"] += 1
        return None
    if info.walled:
        # Bot protection hides the page from us. Keep it (lowest tier) only if the search reported a
        # title and the URL itself has the shape of a single product page.
        if not raw.title or is_non_product_url(final_url) or not has_product_marker(final_url):
            run.stats["unverifiable"] += 1
            return None
        run.stats["walled"] += 1
        image = await _find_image(run, [raw.image_url], final_url)
        return _Found(_build_product(raw, info, final_url, origin, image, run.picks), TIER_UNVERIFIED)
    if info.sold_out:
        run.stats["sold_out"] += 1
        return None
    if not (info.title or raw.title):
        run.stats["unverifiable"] += 1
        return None
    # Must look like ONE product: structured product data on the page, or a product-shaped URL.
    if not info.is_product and (is_non_product_url(final_url) or not has_product_marker(final_url)):
        run.stats["not_product"] += 1
        return None

    image = await _find_image(run, [*info.images, raw.image_url], page.url)
    tier = TIER_PRODUCT_PAGE if info.is_product else TIER_PRODUCT_URL
    return _Found(_build_product(raw, info, final_url, origin, image, run.picks), tier)


def _match_text(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w]+", " ", folded, flags=re.UNICODE).strip()


def _match_selected_pick(text: str, picks: tuple[str, ...]) -> str | None:
    # Return a chosen label only when its label or native-script name occurs as a whole phrase.
    normalized_text = f" {_match_text(text)} "
    for pick in picks:
        selected = pick.partition(" over ")[0].strip()
        label, marker, suffix = selected.partition(" (")
        aliases = [label.strip()]
        if marker and suffix.endswith(")"):
            aliases.append(suffix[:-1].strip())
        for alias in aliases:
            phrase = _match_text(alias)
            if phrase and f" {phrase} " in normalized_text:
                return label.strip()
    return None


def _build_product(raw: RawProduct, info: _PageInfo, final_url: str, origin: Origin, image: str | None,
                   picks: tuple[str, ...] = ()) -> Product:
    """Merge what the search reported with what the page says (page metadata wins for price)."""
    price = format_price(info.price, info.currency) if info.price else None
    currency = _currency_code(info.currency, price) if price else None
    if price is None and raw.price:
        numeric = re.fullmatch(r"[\d.,]+", raw.price.strip())
        price = (format_price(raw.price, raw.currency) if numeric else None) or raw.price[:40]
        currency = _currency_code(raw.currency, price)

    title = (raw.title or info.title or "T-shirt").strip()[:140]
    retailer = (raw.retailer or info.site_name or site_of(final_url)).strip()[:80]
    brand = (raw.brand or info.brand or retailer).strip()[:80]
    why = raw.why.strip()[:240] or "Matches the colors and mood of your profile."
    page_text = "" if info.walled else " ".join(filter(None, (info.title, info.description)))
    matched_pick = _match_selected_pick(page_text, picks)
    match_source = "product_page" if matched_pick else None
    if matched_pick is None:
        matched_pick = _match_selected_pick(raw.title, picks)
        match_source = "search_result" if matched_pick else None
    return Product(
        id=hashlib.sha1(url_key(final_url).encode()).hexdigest()[:12],
        title=title,
        brand=brand,
        retailer=retailer,
        url=final_url,
        image_url=image,
        price=price,
        currency=currency,
        origin=origin,
        why=why,
        pick_match=matched_pick,
        pick_match_source=match_source,
    )


async def _find_image(run: _Run, sources: Iterable[str | None], base: str) -> str | None:
    """The first of ``sources`` (page og:image / twitter:image / JSON-LD images, then the one the
    search reported) that really displays on our page, else None. Bounded by IMAGE_BUDGET_S: a slow
    image CDN costs the product its photo, never the product itself."""
    candidates = [u for u in dict.fromkeys(_absolute_image(s, base) for s in sources) if u][:MAX_IMAGE_TRIES]
    if not candidates:
        return None

    async def first_usable() -> str | None:
        for candidate in candidates:
            usable = await _usable_image(run.http, candidate)
            if usable:
                return usable
        return None

    try:
        image = await asyncio.wait_for(first_usable(), min(IMAGE_BUDGET_S, run.remaining() - 0.5))
    except asyncio.TimeoutError:
        image = None
    run.stats["image" if image else "no_image"] += 1
    return image


async def _usable_image(http: httpx.AsyncClient, url: str) -> str | None:
    """``url`` (after redirects, each hop SSRF-checked) if it serves a raster image of at least
    MIN_IMAGE_PX that a browser will show on our page, else None.

    Checked the way the Shop's ``<img referrerpolicy="no-referrer">`` loads it: no Referer (CDN
    hotlink rules see the same request), and Cross-Origin-Resource-Policy must allow a cross-site
    embed. The page author picks og:image and every visitor's browser loads it, so it must be a
    public address (no blind GET-CSRF against routers); returning the final hop means browsers never
    follow a redirect we haven't vetted. Only the first IMAGE_SNIFF_BYTES are read.
    """
    current = url
    for _ in range(MAX_IMAGE_REDIRECTS + 1):
        try:
            await _check_url(current)
            async with http.stream("GET", current, headers=_IMAGE_HEADERS, timeout=IMAGE_TIMEOUT_S) as resp:
                if resp.status_code in _REDIRECT_CODES and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"].strip())
                    continue
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                corp = resp.headers.get("cross-origin-resource-policy", "").strip().lower()
                if (resp.status_code != 200 or not ctype.startswith("image/") or ctype in _NON_PHOTO_TYPES
                        or corp in ("same-origin", "same-site")):
                    log.info("shop: unusable image %s (HTTP %d, %s%s)", _short(current), resp.status_code,
                             ctype or "no type", f", CORP {corp}" if corp else "")
                    return None
                head = b""
                async for chunk in resp.aiter_bytes():
                    head += chunk
                    if len(head) >= IMAGE_SNIFF_BYTES:
                        break
        except BlockedURL as err:
            log.info("shop: dropped a non-public image %s (%s)", _short(current), err)
            return None
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):  # a bad photo never costs the product
            return None
        size = image_size(head)
        if size is None:  # a format we don't parse (AVIF, odd JPEG): any real photo is > 1 KB
            return current if len(head) >= 1024 else None
        return current if min(size) >= MIN_IMAGE_PX else None
    return None


def image_size(head: bytes) -> tuple[int, int] | None:
    """(width, height) from the first bytes of a PNG / GIF / JPEG / WebP file, else None."""
    try:
        if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
            return struct.unpack(">II", head[16:24])
        if head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
            return struct.unpack("<HH", head[6:10])
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP" and len(head) >= 30:
            chunk = head[12:16]
            if chunk == b"VP8 ":
                width, height = struct.unpack("<HH", head[26:30])
                return width & 0x3FFF, height & 0x3FFF
            if chunk == b"VP8L":
                bits = int.from_bytes(head[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                return int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
            return None
        if head[:2] == b"\xff\xd8":
            pos = 2
            while pos + 9 <= len(head):
                if head[pos] != 0xFF:
                    return None
                marker = head[pos + 1]
                if marker == 0xFF:  # fill byte
                    pos += 1
                    continue
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # start of frame
                    height, width = struct.unpack(">HH", head[pos + 5 : pos + 9])
                    return width, height
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # no length field
                    pos += 2
                    continue
                pos += 2 + struct.unpack(">H", head[pos + 2 : pos + 4])[0]
    except (struct.error, IndexError):
        return None
    return None


async def _validate_into(run: _Run, raw: RawProduct, url: str, origin: Origin) -> None:
    """Validate under the concurrency limit and record the result. Never raises (except cancel)."""
    async with run.semaphore:
        budget = min(FETCH_TIMEOUT_S + IMAGE_BUDGET_S + 2, run.remaining())
        if budget <= 1:
            return
        try:
            found = await asyncio.wait_for(_validate(run, raw, url, origin), budget)
        except (asyncio.TimeoutError, httpx.HTTPError, httpx.InvalidURL, ValueError) as err:
            run.stats["dead"] += 1
            log.info("shop: %s failed (%s)", _short(url), type(err).__name__)
            return
        except Exception:  # noqa: BLE001 - one bad page must never sink the batch
            run.stats["error"] += 1
            log.exception("shop: unexpected error validating %s", _short(url))
            return
    if found is None:
        return
    # Redirects can land two candidates on the same page; titles catch the same shirt at two URLs.
    keys = (url_key(found.product.url), _title_key(found.product.url, found.product.title))
    if keys[0] in run.final_urls or keys[1] in run.titles:
        run.stats["duplicate"] += 1
        return
    run.final_urls.add(keys[0])
    run.titles.add(keys[1])
    run.found[origin].append(found)


class NoCandidates(UpstreamError):
    """A search answered, but with no usable products (counts as a failed attempt)."""


_EMPTY_ARRAY_RE = re.compile(r"^\s*(?:```(?:json)?\s*)?\[\s*\]\s*(?:```)?\s*$")


async def _restructure(text: str, run: _Run) -> list[RawProduct]:
    """The answer wasn't usable JSON: let the cheap model restructure it (no second search)."""
    try:
        return await asyncio.wait_for(
            llm.chat_json(
                get_settings().cheap_model,
                "Extract the t-shirt products listed in the research notes below as JSON. Keep only "
                "facts present in the notes; never invent URLs, prices, or products. " + llm.UNTRUSTED_NOTE,
                llm.wrap_untrusted(text[:30000], "web_search_answer"),
                list[RawProduct],
                max_tokens=4000,
                reasoning_effort="low",
            ),
            timeout=max(5.0, run.remaining() - VALIDATION_RESERVE_S),
        )
    except (LLMError, asyncio.TimeoutError):
        return []


async def _search_once(
    origin: Origin, profile: Profile, country: Country, run: _Run, relaxed: bool = False
) -> list[RawProduct]:
    """One search request -> candidates. Raises LLMError (request failed) or NoCandidates."""
    timeout = max(10.0, min(SEARCH_TIMEOUT_S, run.remaining() - VALIDATION_RESERVE_S))
    settings = get_settings()
    # The backup attempt runs on a different model: a slow primary model is usually slow for all
    # three searches at once, so a second request to the same model rarely wins the race.
    model = (settings.shop_backup_model or settings.shop_model) if relaxed else settings.shop_model
    text = await llm.web_search_text(
        model, _search_prompt(origin, profile, country, relaxed, run.picks),
        instructions=SEARCH_INSTRUCTIONS, timeout=timeout, reasoning_effort=SEARCH_REASONING_EFFORT,
        search_context_size=SEARCH_CONTEXT_SIZE,
    )
    raws = parse_candidates(text)
    if not raws and not _EMPTY_ARRAY_RE.match(text) and run.remaining() > VALIDATION_RESERVE_S + 8:
        raws = await _restructure(text, run)
    if not raws:
        raise NoCandidates(f"the {origin} search found no products")
    return raws


async def _search(origin: Origin, profile: Profile, country: Country, run: _Run) -> list[RawProduct]:
    """Search one origin, hedged against a flaky gateway: if the first attempt hasn't produced
    candidates after HEDGE_AFTER_S, or failed / came back empty, a second independent attempt
    (fresh prompt tag, looser taste match) starts; the first with candidates wins. The other one
    (already paid for) keeps running in ``run.late``: if it answers before the run ends, its
    candidates are validated too (more shirts to choose from, so more of them with a photo)."""
    started = time.monotonic()
    pending = {asyncio.create_task(_search_once(origin, profile, country, run))}
    hedged = False
    errors: list[BaseException] = []
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, timeout=None if hedged else HEDGE_AFTER_S, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task.exception() is None:
                    raws = task.result()
                    log.info("shop: %s search %.1fs -> %d candidates", origin, time.monotonic() - started, len(raws))
                    for attempt in (done - {task}) | pending:
                        run.late.add(asyncio.create_task(_late_candidates(origin, attempt, run)))
                    pending = set()
                    return raws
                errors.append(task.exception())
            if not hedged:
                hedged = True
                if run.remaining() > VALIDATION_RESERVE_S + 15:
                    run.stats["hedged"] += 1
                    log.info("shop: %s search %s; starting a backup request", origin,
                             f"failed ({errors[-1]})" if errors else "is slow")
                    pending.add(asyncio.create_task(_search_once(origin, profile, country, run, relaxed=True)))
        raise errors[0]
    finally:
        for task in pending:
            task.cancel()


async def _run_origin(origin: Origin, profile: Profile, country: Country, run: _Run) -> None:
    """Search one origin, then validate its candidates concurrently as soon as they arrive."""
    raws = await _search(origin, profile, country, run)
    run.answered.add(origin)
    await _validate_candidates(origin, raws, run)


async def _late_candidates(origin: Origin, attempt: asyncio.Task[list[RawProduct]], run: _Run) -> None:
    """Validate the answer of a hedged search attempt that lost its race, if it brings one."""
    try:
        raws = await attempt
    except (LLMError, UpstreamError):
        return
    log.info("shop: late %s search answer -> %d candidates", origin, len(raws))
    run.stats["late_answer"] += 1
    await _validate_candidates(origin, raws, run)


async def _validate_candidates(origin: Origin, raws: list[RawProduct], run: _Run) -> None:
    jobs: list[tuple[RawProduct, str]] = []
    for raw in raws[: CANDIDATES_PER_SEARCH + 4]:
        url = clean_url(raw.url)
        if not url or is_non_product_url(url):
            run.stats["screened"] += 1
            continue
        key = url_key(url)
        if key in run.claimed:
            run.stats["duplicate"] += 1
            continue
        run.claimed.add(key)
        jobs.append((raw, url))
    async with asyncio.TaskGroup() as group:
        for raw, url in jobs:
            group.create_task(_validate_into(run, raw, url, origin_for(url, origin)))


def balance(found: dict[str, list[_Found]], limit: int = MAX_RESULTS) -> list[Product]:
    """Pick up to ``limit`` products, origins interleaved round-robin (local, global, indie, ...),
    best validation tier first within each origin, at most ``PER_DOMAIN_CAP`` per store (relaxed
    if that leaves fewer than MIN_RESULTS).

    Shirts with a product photo are picked first. Shirts without one (mostly bot-walled retailers,
    where we can't see the page) then fill in only: the best one of an origin that has no
    photographed shirt, as many as it takes to reach MIN_RESULTS, and otherwise at most
    MAX_PHOTOLESS_SHARE of the list. An image shared by two products is a store logo or banner,
    not a product photo, and counts as none."""
    images = Counter(f.product.image_url for found_list in found.values() for f in found_list if f.product.image_url)
    ranked = {
        o: [
            f.product.model_copy(update={"image_url": None}) if images[f.product.image_url] > 1 else f.product
            for f in sorted(found.get(o, []), key=lambda f: (f.tier, f.product.pick_match is None))
        ]
        for o in ORIGINS
    }
    photoless = {o: [p for p in ranked[o] if not p.image_url] for o in ORIGINS}
    bare = sum(1 for o in ORIGINS if photoless[o] and len(photoless[o]) == len(ranked[o]))  # no photo at all
    picked = _round_robin({o: [p for p in ranked[o] if p.image_url] for o in ORIGINS}, [], limit - bare)
    room = max(MIN_RESULTS - len(picked), int(len(picked) * MAX_PHOTOLESS_SHARE / (1 - MAX_PHOTOLESS_SHARE) + 1e-9))
    for origin in ORIGINS:
        if photoless[origin] and len(picked) < limit and all(p.origin != origin for p in picked):
            picked.append(photoless[origin].pop(0))
            room -= 1
    _round_robin(photoless, picked, min(limit, len(picked) + max(0, room)))
    by_origin = {o: [p for p in picked if p.origin == o] for o in ORIGINS}
    return [p for row in zip_longest(*by_origin.values()) for p in row if p is not None]


def _round_robin(queues: dict[str, list[Product]], picked: list[Product], limit: int) -> list[Product]:
    """Append products from ``queues`` to ``picked`` taking turns across origins, at most ``cap``
    per store, the cap relaxed step by step while ``picked`` is short of MIN_RESULTS."""
    for cap in (PER_DOMAIN_CAP, PER_DOMAIN_CAP * 2, limit):
        per_store = Counter(store_of(p.url) for p in picked)
        pending = {o: [p for p in queues.get(o, []) if p not in picked] for o in ORIGINS}
        progress = True
        while len(picked) < limit and progress:
            progress = False
            for origin in ORIGINS:
                queue = pending[origin]
                while queue and len(picked) < limit:
                    product = queue.pop(0)
                    shop_id = store_of(product.url)
                    if per_store[shop_id] < cap:
                        picked.append(product)
                        per_store[shop_id] += 1
                        progress = True
                        break
        if len(picked) >= min(MIN_RESULTS, limit):
            break
    return picked


# One in-flight search per (session, profile): a page reload (or a second tab) mid-search joins the
# running one instead of paying for 3-6 more web searches. A changed profile starts a new search.
_inflight: dict[tuple[str, str], asyncio.Task[list[Product]]] = {}


def _forget(key: tuple[str, str], task: asyncio.Task[list[Product]]) -> None:
    if _inflight.get(key) is task:
        del _inflight[key]
    if not task.cancelled():
        task.exception()  # mark retrieved; awaiting callers re-raise it themselves


async def suggest(session_id: str) -> list[Product]:
    """Find 8-12 real, currently purchasable t-shirts that fit the session's profile.

    Every call re-runs the searches ("Refresh suggestions"), except that a call arriving while a
    search for the same session and profile is still running joins it. The result is cached
    (HIDDEN) as ``rec.products``. Partial results are returned if the time budget runs out, and
    once MIN_RESULTS products are verified and two of the three origins are done, the last origin
    gets only SHOP_GRACE_S more (a hung search used to hold finished results back for ~45 s).

    Raises:
        NotFoundError: unknown session.
        ConflictError: no profile yet.
        LLMError: every search failed.
        UpstreamError: searches ran but no usable product page came back.
    """
    rec = store.require_session(session_id)
    if rec.profile is None:
        raise ConflictError("Reveal your profile first.")
    key = (session_id, hashlib.sha1(rec.profile.model_dump_json().encode()).hexdigest())
    loop = asyncio.get_running_loop()
    task = _inflight.get(key)
    if task is None or task.done() or task.get_loop() is not loop:
        picks = picks_from_answers(rec.answers)
        task = loop.create_task(_suggest(session_id, rec.profile, rec.country, picks), name=f"shop:{session_id}")
        _inflight[key] = task
        task.add_done_callback(partial(_forget, key))
    # shield: one caller disconnecting must not cancel the search others are waiting on
    return await asyncio.shield(task)


async def _grace(run: _Run, pending: set[asyncio.Task[None]]) -> None:
    """Wait up to SHOP_GRACE_S for the ``pending`` origin searches and, while shirts with a photo
    are short of MIN_RESULTS, for hedged attempts that lost their race (already paid for)."""
    waiting = set(pending)
    if run.photographed() < MIN_RESULTS:
        late = {task for task in run.late if not task.done()}
        if late:
            log.info("shop: %d shirts with a photo; waiting for %d late search answer(s) too",
                     run.photographed(), len(late))
        waiting |= late
    if waiting and run.remaining() > 0:
        await asyncio.wait(waiting, timeout=min(SHOP_GRACE_S, run.remaining()))


async def _suggest(
    session_id: str, profile: Profile, country: Country, picks: tuple[str, ...] = ()
) -> list[Product]:
    started = time.monotonic()
    async with _make_client() as http:
        run = _Run(
            http=http, deadline=started + TOTAL_BUDGET_S, semaphore=asyncio.Semaphore(VALIDATION_CONCURRENCY),
            picks=picks,
        )
        tasks = {asyncio.create_task(_run_origin(o, profile, country, run)): o for o in ORIGINS}
        pending: set[asyncio.Task[None]] = set(tasks)
        while pending:
            finished, pending = await asyncio.wait(
                pending, timeout=max(1.0, run.remaining()), return_when=asyncio.FIRST_COMPLETED
            )
            if not finished:
                break  # total budget reached
            verified = sum(len(found) for found in run.found.values())
            if pending and len(tasks) - len(pending) >= 2 and verified >= MIN_RESULTS:
                log.info(
                    "shop: %d verified with %s still searching; giving it %.0fs more",
                    verified, ", ".join(tasks[t] for t in pending), SHOP_GRACE_S,
                )
                await _grace(run, pending)
                # A search that answered within the grace gets a moment to check its links: cutting
                # it off mid-validation would throw away shirts that are already paid for.
                checking = {task for task in pending if not task.done() and tasks[task] in run.answered}
                if checking and run.remaining() > 0:
                    await asyncio.wait(checking, timeout=min(LINK_CHECK_GRACE_S, run.remaining()))
                pending = {task for task in pending if not task.done()}
                break
        else:  # every origin answered: lost hedged attempts may still bring shirts with a photo
            await _grace(run, set())
        for task in pending:
            task.cancel()
        if pending:
            log.info("shop: not waiting for %s", ", ".join(tasks[t] for t in pending))
            await asyncio.gather(*pending, return_exceptions=True)
        for task in run.late:
            task.cancel()
        await asyncio.gather(*run.late, return_exceptions=True)

    failures: list[BaseException] = []
    for task in tasks:
        if task in pending or task.cancelled():
            continue
        exc = task.exception()
        if exc is not None:
            failures.append(exc)
            log.warning("shop: %s search failed: %s", tasks[task], exc)
    products = balance(run.found)
    log.info(
        "shop: session %s -> %d products in %.1fs (found %s; %s)",
        session_id, len(products), time.monotonic() - started,
        {o: len(run.found[o]) for o in ORIGINS}, dict(run.stats),
    )
    if not products:
        if failures and len(failures) == len(ORIGINS) and all(isinstance(f, LLMError) for f in failures):
            raise LLMError("The shirt search hit a snag. Please try again.")
        raise UpstreamError("We couldn't verify any shirts right now. Please try again in a moment.")

    async with store.edit_session(session_id) as live:
        live.products = products
    return products
