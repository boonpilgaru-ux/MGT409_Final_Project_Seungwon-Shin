"""shop.py: search orchestration, candidate parsing, URL hygiene, SSRF guard, page validation, balancing.

Network is simulated: ``shop._make_transport`` -> httpx.MockTransport serving a tiny fake web, and
``shop._resolve_ips`` -> a fake DNS table (so no real lookups happen either).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import struct
from collections import Counter

import httpcore
import httpx
import pytest

from app import llm, shop, store
from app.errors import LLMError
from app.models import Product
from app.models import Country
from tests.factories import make_profile, seed_session

# Captured at import, before conftest's autouse fixture swaps in the "network disabled" stub.
_REAL_HANDLE_ASYNC = httpx.AsyncHTTPTransport.handle_async_request

# --------------------------------------------------------------------------- fake DNS + web

DNS = {
    "kr.brand.example": "93.184.216.10",
    "shop.example": "93.184.216.11",
    "global.example": "93.184.216.12",
    "indie.example": "93.184.216.13",
    "cdn.example": "93.184.216.14",
    "img.example": "93.184.216.15",
    "evil.example": "10.0.0.7",
    "rebind.example": ["93.184.216.16", "127.0.0.1"],
    "metadata.example": "169.254.169.254",
    "www.etsy.com": "93.184.216.17",
}


async def fake_resolve(host: str, port: int):
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    if host not in DNS:
        raise OSError(f"NXDOMAIN {host}")
    value = DNS[host]
    return [ipaddress.ip_address(v) for v in (value if isinstance(value, list) else [value])]


def page(*, title="Tee", og_type="product", image=None, twitter=None, price=None, currency=None,
         jsonld=None, body="") -> str:
    meta = [f'<meta property="og:type" content="{og_type}">'] if og_type else []
    meta.append(f'<meta property="og:title" content="{title}">')
    if image:
        meta.append(f'<meta property="og:image" content="{image}">')
    if twitter:
        meta.append(f'<meta name="twitter:image" content="{twitter}">')
    if price:
        meta.append(f'<meta property="product:price:amount" content="{price}">')
    if currency:
        meta.append(f'<meta property="product:price:currency" content="{currency}">')
    ld = f'<script type="application/ld+json">{json.dumps(jsonld)}</script>' if jsonld else ""
    return f"<html><head><title>{title}</title>{''.join(meta)}</head><body>{body}{ld}</body></html>"


HTML = {"content-type": "text/html; charset=utf-8"}


def png(width: int = 800, height: int = 800) -> bytes:
    """A PNG signature + IHDR of the given size, padded like a real photo (only the header is read)."""
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\x08\x02" + b"\0" * 2048


def photo(width: int = 800, height: int = 800, **headers) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "image/png", **headers}, content=png(width, height))


PAGES: dict[str, httpx.Response] = {
    # product photos (og:image / twitter:image / JSON-LD / search-reported)
    "https://kr.brand.example/img/tee.jpg": photo(),
    "https://cdn.example/h.jpg": photo(),
    "https://img.example/ut.jpg": photo(),
    "https://cdn.example/riso.jpg": photo(),
    "https://img.example/model-said.jpg": photo(600, 600),
    "https://img.example/plain-said.jpg": photo(),
    # local
    "https://kr.brand.example/products/night-market-tee": httpx.Response(
        200, headers=HTML, text=page(title="Night Market Tee | Brand", image="/img/tee.jpg", price="39000",
                                     currency="KRW")),
    "https://kr.brand.example/products/hangul-tee": httpx.Response(
        200, headers=HTML, text=page(title="Hangul Tee", image="https://cdn.example/h.jpg")),
    "https://kr.brand.example/products/gone": httpx.Response(404, headers=HTML, text="nope"),
    "https://shop.example/products/old-tee": httpx.Response(301, headers={"location": "/kr/"}),
    "https://shop.example/kr/": httpx.Response(200, headers=HTML, text=page(title="Shop home", og_type="website")),
    "https://kr.brand.example/products/walled-tee": httpx.Response(
        403, headers={"server": "cloudflare", "content-type": "text/html"}, text="<title>Attention Required!</title>"),
    "https://kr.brand.example/products/forbidden-tee": httpx.Response(403, headers=HTML, text="Forbidden"),
    "https://kr.brand.example/products/soft-404": httpx.Response(
        200, headers=HTML, text=page(title="Page Not Found | Brand", og_type="website")),
    "https://kr.brand.example/about-walled": httpx.Response(
        403, headers={"x-datadome": "protected", "content-type": "text/html"}, text="blocked"),
    "https://shop.example/products/sold-out-tee": httpx.Response(200, headers=HTML, text=page(
        title="Sold out tee", jsonld={"@context": "https://schema.org", "@type": "Product", "name": "x",
                                      "offers": {"@type": "Offer", "availability": "https://schema.org/OutOfStock",
                                                 "price": "30"}})),
    # global
    "https://global.example/item/123456": httpx.Response(200, headers=HTML, text=page(
        title="UT Graphic Tee", og_type=None, jsonld={"@graph": [{"@type": "WebSite"}, {
            "@type": "Product", "name": "UT Graphic Tee", "image": ["//img.example/ut.jpg"],
            "brand": {"@type": "Brand", "name": "Uniqlo"},
            "offers": [{"@type": "Offer", "price": 19.9, "priceCurrency": "USD",
                        "availability": "https://schema.org/InStock"}]}]})),
    "https://global.example/item/777777": httpx.Response(302, headers={"location": "https://global.example/item/123456"}),
    "https://global.example/pdf/555555": httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF"),
    "https://global.example/item/888888": httpx.Response(200, headers=HTML, text=page(title="Just a moment...", og_type=None)),
    "https://global.example/about-us": httpx.Response(200, headers=HTML, text=page(title="About", og_type="website")),
    # indie
    "https://indie.example/listing/554433/riso-tee": httpx.Response(200, headers=HTML, text=page(
        title="Riso Tiger Tee", og_type="og:product", twitter="//cdn.example/riso.jpg")),
    "https://indie.example/listing/998877/redirect-tee": httpx.Response(302, headers={"location": "https://evil.example/x"}),
    "https://indie.example/listing/112233/meta-tee": httpx.Response(
        302, headers={"location": "http://metadata.example/latest/meta-data"}),
    "https://indie.example/listing/445566/no-image-tee": httpx.Response(200, headers=HTML, text=page(title="Plain Tee")),
    "https://www.etsy.com/listing/7654321/hotteok-tee": httpx.Response(
        403, headers={"server": "DataDome", "x-datadome": "protected", "content-type": "text/html"}, text="blocked"),
}


class FakeWeb:
    def __init__(self) -> None:
        self.requested: list[str] = []
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        self.requests.append(request)
        assert request.headers["user-agent"] == shop.USER_AGENT
        resp = PAGES.get(url)
        if resp is None:
            return httpx.Response(404, headers=HTML, text="not found")
        return httpx.Response(resp.status_code, headers=resp.headers, content=resp.content)


@pytest.fixture
def web(monkeypatch) -> FakeWeb:
    fake = FakeWeb()
    monkeypatch.setattr(shop, "_make_transport", lambda: httpx.MockTransport(fake.handler))
    monkeypatch.setattr(shop, "_resolve_ips", fake_resolve)
    return fake


def item(url: str, title: str, **extra) -> dict:
    return {"title": title, "brand": extra.pop("brand", "Brand"), "retailer": extra.pop("retailer", "Store"),
            "url": url, "image_url": extra.pop("image_url", None), "price": extra.pop("price", None),
            "currency": extra.pop("currency", None), "why": extra.pop("why", f"You'd love {title}."), **extra}


SEARCH_RESULTS = {
    "local": [
        item("https://kr.brand.example/products/night-market-tee?utm_source=openai", "Night Market Tee",
             price="₩35,000", currency="KRW"),
        item("https://kr.brand.example/products/hangul-tee#reviews", "Hangul Tee", price="₩42,000"),
        item("https://kr.brand.example/products/gone", "Gone Tee"),
        item("https://shop.example/products/old-tee", "Old Tee"),
        item("https://shop.example/products/sold-out-tee", "Sold Out Tee"),
        item("https://kr.brand.example/products/walled-tee", "Walled Tee", price="39000", currency="KRW",
             image_url="https://img.example/model-said.jpg"),
        item("https://kr.brand.example/products/forbidden-tee", "Forbidden Tee"),
        item("https://kr.brand.example/products/soft-404", "Soft 404 Tee"),
        item("https://kr.brand.example/about-walled", "About (walled)"),
        item("https://shop.example/collections/tees", "All tees"),
        item("https://www.instagram.com/p/abc123/", "Insta post"),
        item("http://127.0.0.1/products/x", "Loopback"),
        {"title": "no url"},
        "not even a dict",
    ],
    "global": [
        item("https://global.example/item/123456", "UT Graphic Tee", brand="Uniqlo"),
        # Same shirt at an old URL that redirects to the one above: must collapse into one product.
        item("https://global.example/item/777777", "UT Graphic Tee", brand="Uniqlo"),
        item("https://global.example/pdf/555555", "Size chart"),
        item("https://global.example/item/888888", "Bot walled"),
        item("https://global.example/about-us", "About"),
        item("https://kr.brand.example/products/hangul-tee", "Hangul Tee dup"),
        item("https://www.etsy.com/listing/7654321/hotteok-tee", "Hotteok Tee"),  # maker marketplace
    ],
    "indie": [
        {"name": "Riso Tiger Tee", "link": "[Riso](https://indie.example/listing/554433/riso-tee)",
         "store": "Etsy", "maker": {"name": "Riso Studio"}, "price": 28, "currency": "usd",
         "reason": "Hand-printed tigers for your night-owl streak."},
        item("https://indie.example/listing/998877/redirect-tee", "Redirect Tee"),
        item("https://indie.example/listing/112233/meta-tee", "Metadata Tee"),
        item("https://indie.example/listing/445566/no-image-tee", "Plain Tee",
             image_url="https://img.example/plain-said.jpg"),
    ],
}

FOCUS = {"local": "Search focus: local brands", "global": "Search focus: global brands",
         "indie": "Search focus: independent makers"}


def origin_of(prompt: str) -> str:
    return next(o for o, marker in FOCUS.items() if marker in prompt)


def run_suggest(session_id: str) -> list[Product]:
    return asyncio.run(shop.suggest(session_id))


# --------------------------------------------------------------------------- end to end


def test_suggest_end_to_end(fake_llm, web, client):
    rec = seed_session(done=True, profile=True)
    fake_llm.on("web_search", lambda call: json.dumps(SEARCH_RESULTS[origin_of(call.user)]))

    res = client.post(f"/api/shop/{rec.id}/suggest")
    assert res.status_code == 200
    products = [Product(**p) for p in res.json()["products"]]
    by_title = {p.title: p for p in products}
    assert set(by_title) == {"Night Market Tee", "Hangul Tee", "Walled Tee", "UT Graphic Tee", "Bot walled",
                             "Riso Tiger Tee", "Plain Tee", "Hotteok Tee"}
    assert by_title["Hotteok Tee"].origin == "indie"  # found by the global search, relabelled by domain

    calls = fake_llm.calls_of("web_search_text")
    assert len(calls) == 3 and {origin_of(c.user) for c in calls} == {"local", "global", "indie"}
    assert all(c.model == "gpt-6-luna" and c.kwargs["instructions"] == shop.SEARCH_INSTRUCTIONS for c in calls)
    assert all(rec.profile.tshirt_brief in c.user and "South Korea" in c.user for c in calls)

    night = by_title["Night Market Tee"]
    assert night.url == "https://kr.brand.example/products/night-market-tee"  # utm stripped
    assert night.image_url == "https://kr.brand.example/img/tee.jpg"  # relative og:image resolved
    assert (night.price, night.currency, night.origin) == ("₩39,000", "KRW", "local")  # page price wins
    assert by_title["Hangul Tee"].url == "https://kr.brand.example/products/hangul-tee"  # fragment dropped
    assert by_title["Hangul Tee"].price == "₩42,000" and by_title["Hangul Tee"].currency == "KRW"

    ut = by_title["UT Graphic Tee"]
    assert (ut.image_url, ut.price, ut.currency, ut.origin) == ("https://img.example/ut.jpg", "$19.90", "USD", "global")

    riso = by_title["Riso Tiger Tee"]
    assert (riso.brand, riso.retailer, riso.origin) == ("Riso Studio", "Etsy", "indie")
    assert riso.url == "https://indie.example/listing/554433/riso-tee"
    assert riso.image_url == "https://cdn.example/riso.jpg"  # twitter:image, protocol-relative
    assert (riso.price, riso.currency) == ("$28.00", "USD")
    assert riso.why == "Hand-printed tigers for your night-owl streak."
    assert by_title["Plain Tee"].image_url == "https://img.example/plain-said.jpg"  # verified model image

    # Bot-walled product pages are kept as unverified, built from the search data, ranked last per origin.
    walled = by_title["Walled Tee"]
    assert (walled.url, walled.price, walled.currency) == ("https://kr.brand.example/products/walled-tee",
                                                           "₩39,000", "KRW")
    assert walled.image_url == "https://img.example/model-said.jpg"
    order = [p.title for p in products]
    assert order.index("Walled Tee") > max(order.index("Night Market Tee"), order.index("Hangul Tee"))
    assert order.index("Bot walled") > order.index("UT Graphic Tee")  # 200 challenge page = walled too

    # Screened before fetching: collection page, social post, loopback; private redirects never followed.
    assert not any("collections" in u or "instagram" in u or "127.0.0.1" in u for u in web.requested)
    assert not any("evil.example" in u or "metadata.example" in u for u in web.requested)
    # The duplicate Hangul Tee from the global search was claimed once only.
    assert sum(u.startswith("https://kr.brand.example/products/hangul-tee") for u in web.requested) == 1

    assert len({p.id for p in products}) == len(products)
    assert [p.origin for p in products[:3]] == ["local", "global", "indie"]  # interleaved
    assert store.require_session(rec.id).products == products  # cached (hidden)
    assert "products" not in client.get(f"/api/session/{rec.id}").json()


def test_product_ids_are_stable_across_refreshes(fake_llm, web):
    rec = seed_session(done=True, profile=True)
    fake_llm.on("web_search", lambda call: json.dumps(SEARCH_RESULTS[origin_of(call.user)]))
    first = run_suggest(rec.id)
    second = run_suggest(rec.id)
    assert {p.url: p.id for p in first} == {p.url: p.id for p in second}
    calls = fake_llm.calls_of("web_search_text")
    assert len(calls) == 6  # refresh re-runs every search...
    local_prompts = [c.user for c in calls if origin_of(c.user) == "local"]
    assert local_prompts[0] != local_prompts[1]  # ...with a unique prompt, so gateway caching can't replay it


def test_envelope_and_prose_answers_are_parsed(fake_llm, web):
    rec = seed_session(done=True, profile=True)
    answers = {
        "local": "Here are some picks:\n```json\n" + json.dumps({"products": SEARCH_RESULTS["local"][:1]}) + "\n```",
        "global": json.dumps({"results": SEARCH_RESULTS["global"][:1]}),
        "indie": "Sorry, I could not find anything in a structured way.",
    }
    fake_llm.on("web_search", lambda call: answers[origin_of(call.user)])
    fake_llm.on("chat_json", lambda call: [SEARCH_RESULTS["indie"][3]])  # cheap-model restructuring
    products = run_suggest(rec.id)
    assert {p.title for p in products} == {"Night Market Tee", "UT Graphic Tee", "Plain Tee"}
    [conversion] = fake_llm.calls_of("chat_json")
    assert conversion.model == "gpt-6-luna" and "<untrusted_data" in conversion.user


def test_one_failed_search_still_returns_others(fake_llm, web):
    rec = seed_session(done=True, profile=True)

    def handler(call):
        if origin_of(call.user) == "global":
            return LLMError("The web search failed. Please try again.")
        return json.dumps(SEARCH_RESULTS[origin_of(call.user)])

    fake_llm.on("web_search", handler)
    products = run_suggest(rec.id)
    assert {p.origin for p in products} == {"local", "indie"} and len(products) == 5
    calls = [origin_of(c.user) for c in fake_llm.calls_of("web_search_text")]
    assert Counter(calls) == {"local": 1, "global": 2, "indie": 1}  # failure triggered one backup request


def test_failed_search_recovers_with_backup_request(fake_llm, web):
    rec = seed_session(done=True, profile=True)
    attempts: Counter = Counter()

    def handler(call):
        origin = origin_of(call.user)
        attempts[origin] += 1
        if origin == "indie" and attempts[origin] == 1:
            return LLMError("The web search failed. Please try again.")
        return json.dumps(SEARCH_RESULTS[origin])

    fake_llm.on("web_search", handler)
    products = run_suggest(rec.id)
    assert attempts == {"local": 1, "global": 1, "indie": 2}
    assert {"Riso Tiger Tee", "Plain Tee"} <= {p.title for p in products}


def test_empty_answer_triggers_backup_without_restructuring(fake_llm, web):
    rec = seed_session(done=True, profile=True)
    attempts: Counter = Counter()

    def handler(call):
        origin = origin_of(call.user)
        attempts[origin] += 1
        if origin == "local" and attempts[origin] == 1:
            return "```json\n[]\n```"  # the model gave up; no point asking the cheap model to convert it
        return json.dumps(SEARCH_RESULTS[origin])

    fake_llm.on("web_search", handler)
    products = run_suggest(rec.id)
    assert attempts["local"] == 2 and not fake_llm.calls_of("chat_json")
    assert "Night Market Tee" in {p.title for p in products}
    first, backup = [c.user for c in fake_llm.calls_of("web_search_text") if origin_of(c.user) == "local"]
    assert "second attempt" not in first and "second attempt" in backup  # backup loosens the taste match


def test_search_that_stays_empty_fails_that_origin_only(fake_llm, web):
    rec = seed_session(done=True, profile=True)
    fake_llm.on("web_search", lambda call: "[]" if origin_of(call.user) == "indie"
                else json.dumps(SEARCH_RESULTS[origin_of(call.user)]))
    products = run_suggest(rec.id)
    assert not {"Riso Tiger Tee", "Plain Tee"} & {p.title for p in products}
    assert len([c for c in fake_llm.calls_of("web_search_text") if origin_of(c.user) == "indie"]) == 2


@pytest.mark.parametrize(("text", "empty"), [("[]", True), ("```json\n[ ]\n```", True), ("[\n]", True),
                                             ('[{"url": "x"}]', False), ("none found", False)])
def test_empty_array_detection(text, empty):
    assert bool(shop._EMPTY_ARRAY_RE.match(text)) is empty


def test_slow_search_is_hedged(web, monkeypatch):
    rec = seed_session(done=True, profile=True)
    monkeypatch.setattr(shop, "HEDGE_AFTER_S", 0.2)
    calls: list[tuple[str, str]] = []

    async def search(model, prompt, **kwargs):
        origin = origin_of(prompt)
        calls.append((origin, prompt))
        assert kwargs["reasoning_effort"] == shop.SEARCH_REASONING_EFFORT
        if origin == "global" and sum(o == "global" for o, _ in calls) == 1:
            await asyncio.sleep(30)  # the first global request hangs; the backup must win
        return json.dumps(SEARCH_RESULTS[origin])

    monkeypatch.setattr(llm, "web_search_text", search)
    products, elapsed = asyncio.run(_timed(rec.id))
    global_prompts = [prompt for origin, prompt in calls if origin == "global"]
    assert len(global_prompts) == 2 and global_prompts[0] != global_prompts[1]
    assert Counter(o for o, _ in calls) == {"local": 1, "global": 2, "indie": 1}
    assert "UT Graphic Tee" in {p.title for p in products} and elapsed < 10


def test_backup_search_uses_the_backup_model(web, monkeypatch):
    from app.config import get_settings

    rec = seed_session(done=True, profile=True)
    monkeypatch.setattr(shop, "HEDGE_AFTER_S", 0.2)
    settings = get_settings()
    calls: list[tuple[str, str, dict]] = []
    indie_attempts = 0

    async def search(model, prompt, **kwargs):
        nonlocal indie_attempts
        calls.append((origin_of(prompt), model, kwargs))
        if origin_of(prompt) == "indie":
            indie_attempts += 1
            if indie_attempts == 1:
                raise LLMError("The web search failed. Please try again.")  # first Luna attempt fails
        return json.dumps(SEARCH_RESULTS[origin_of(prompt)])

    monkeypatch.setattr(llm, "web_search_text", search)
    products = run_suggest(rec.id)
    indie_models = [model for origin, model, _ in calls if origin == "indie"]
    assert indie_models == [settings.shop_model, settings.shop_backup_model]
    assert all(kw["search_context_size"] == shop.SEARCH_CONTEXT_SIZE for _, _, kw in calls)
    assert "Riso Tiger Tee" in {p.title for p in products}


def test_all_searches_failing_is_502(fake_llm, web, client):
    rec = seed_session(done=True, profile=True)
    fake_llm.on("web_search", lambda call: LLMError("The web search took too long. Please try again."))
    res = client.post(f"/api/shop/{rec.id}/suggest")
    assert res.status_code == 502 and res.json()["detail"] == "The shirt search hit a snag. Please try again."


def test_nothing_verifiable_is_502(fake_llm, web, client):
    rec = seed_session(done=True, profile=True)
    fake_llm.on("web_search", lambda call: json.dumps([item("https://kr.brand.example/products/gone", "Gone")]))
    res = client.post(f"/api/shop/{rec.id}/suggest")
    assert res.status_code == 502 and "verify" in res.json()["detail"]
    assert store.require_session(rec.id).products == []


def test_budget_returns_partial_results(fake_llm, web, monkeypatch):
    rec = seed_session(done=True, profile=True)
    monkeypatch.setattr(shop, "TOTAL_BUDGET_S", 1.5)
    monkeypatch.setattr(shop, "VALIDATION_RESERVE_S", 0.5)
    hung = asyncio.Event()

    async def search(model, prompt, **kwargs):
        if origin_of(prompt) == "indie":
            hung.set()
            await asyncio.sleep(30)  # never answers within the budget
        return json.dumps(SEARCH_RESULTS[origin_of(prompt)])

    monkeypatch.setattr(llm, "web_search_text", search)
    products, elapsed = asyncio.run(_timed(rec.id))
    assert hung.is_set() and elapsed < 5
    titles = {p.title for p in products}
    assert {"Night Market Tee", "UT Graphic Tee"} <= titles  # local + global made it in time
    assert not titles & {"Riso Tiger Tee", "Plain Tee"}  # the indie search never answered


async def _timed(session_id: str):
    loop = asyncio.get_running_loop()
    start = loop.time()
    products = await shop.suggest(session_id)
    return products, loop.time() - start


def test_requires_profile():
    from app.errors import ConflictError

    rec = seed_session(done=True)
    with pytest.raises(ConflictError):
        run_suggest(rec.id)


# --------------------------------------------------------------------------- balancing


def _found(i: int, origin: str, site: str = "", tier: int = shop.TIER_PRODUCT_PAGE, image: str | None = "own"):
    site = site or f"site{i}.example"
    image = f"https://{site}/img/{origin}{i}.jpg" if image == "own" else image
    product = Product(id=f"{origin}{i}", title=f"T{i}", brand="B", retailer="R", url=f"https://{site}/p/{origin}{i}",
                      image_url=image, origin=origin, why="w")
    return shop._Found(product=product, tier=tier)


def test_balance_interleaves_and_caps():
    found = {
        "local": [_found(i, "local") for i in range(10)],
        "global": [_found(20, "global")],
        "indie": [_found(30, "indie"), _found(31, "indie")],
    }
    picked = shop.balance(found)
    assert len(picked) == 12
    assert [p.origin for p in picked[:5]] == ["local", "global", "indie", "local", "indie"]
    assert sum(p.origin == "local" for p in picked) == 9


def test_balance_prefers_better_tiers_and_limits_per_site():
    found = {
        "local": [_found(0, "local", tier=shop.TIER_UNVERIFIED), _found(1, "local", tier=shop.TIER_PRODUCT_URL),
                  _found(2, "local")],
        "global": [_found(i, "global", site="bigshop.example") for i in range(10, 16)],
        "indie": [_found(i, "indie", site="etsy.com") for i in range(20, 26)],
    }
    picked = shop.balance(found)
    local_order = [p.id for p in picked if p.origin == "local"]
    assert local_order == ["local2", "local1", "local0"]  # product page, then product URL, then unverified
    # Cap of 2 per site would give only 6; relaxed to 4 to reach the minimum of 8.
    assert len(picked) >= shop.MIN_RESULTS
    assert sum(shop.site_of(p.url) == "bigshop.example" for p in picked) <= 4


def test_balance_small_pool_returns_everything():
    found = {"local": [_found(1, "local")], "global": [], "indie": [_found(2, "indie")]}
    assert [p.id for p in shop.balance(found)] == ["local1", "indie2"]


# --------------------------------------------------------------------------- parsing + URL hygiene


def test_parse_candidates_ignores_a_citation_marker_before_the_list():
    products = shop.parse_candidates('Found these [1]: [{"title":"T","url":"https://x.example/products/1","price":"$20"}]')
    assert [p.title for p in products] == ["T"]


def test_parse_candidates_variants():
    fenced = '```json\n[{"title": "A", "url": "https://a.example/p/1", "price": 30}]\n```'
    assert shop.parse_candidates(fenced)[0].price == "30"
    envelope = json.dumps({"items": [{"name": "B", "product_url": "https://b.example/p/2", "brand": {"name": "Bee"}}]})
    [b] = shop.parse_candidates(envelope)
    assert (b.title, b.url, b.brand) == ("B", "https://b.example/p/2", "Bee")
    single = json.dumps({"title": "C", "url": "https://c.example/p/3", "image": ["https://c.example/i.jpg"]})
    assert shop.parse_candidates(single)[0].image_url == "https://c.example/i.jpg"
    assert shop.parse_candidates("no json here") == []
    assert shop.parse_candidates('[{"title": "no url"}, 5, "x"]') == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://a.example/p/1?utm_source=openai&utm_medium=x", "https://a.example/p/1"),
        ("https://a.example/p/1?variant=42&ref=abc&gclid=1#reviews", "https://a.example/p/1?variant=42"),
        ("[Shirt](https://a.example/p/1).", "https://a.example/p/1"),
        ("<https://a.example/p/1>", "https://a.example/p/1"),
        ("//a.example/p/1", "https://a.example/p/1"),
        ("ftp://a.example/p/1", None),
        ("javascript:alert(1)", None),
        ("not a url", None),
        ("https:///nohost", None),
    ],
)
def test_clean_url(raw, expected):
    assert shop.clean_url(raw) == expected


@pytest.mark.parametrize(
    ("url", "non_product"),
    [
        ("https://shop.example/", True),
        ("https://shop.example/search?q=tee", True),
        ("https://www.amazon.com/s?k=graphic+tee", True),
        ("https://shop.example/collections/tees", True),
        ("https://shop.example/collections/tees/products/tiger-tee", False),
        ("https://www.etsy.com/c/clothing/mens-clothing/shirts-and-t-shirts", True),
        ("https://www.etsy.com/listing/1234567890/riso-tiger-tee", False),
        ("https://www.musinsa.com/products/3456789", False),
        ("https://www.uniqlo.com/kr/ko/products/E465185-000/00", False),
        ("https://www.amazon.com/Graphic-Tee/dp/B0ABCDEF12", False),
        ("https://brand.example/blog/our-new-tees", True),
        ("https://brand.example/pages/sale-promo-codes?variant=41988603150407", True),  # Shopify CMS page
        ("https://brand.example/shop", True),
        ("https://brand.example/t-shirts", True),
        ("https://brand.example/shop/riso-tiger-tee", False),
        ("https://www.pinterest.com/pin/12345678/", True),
        ("https://www.google.co.kr/search?q=tee", True),
        ("https://m.facebook.com/shop/item/123456", True),
    ],
)
def test_is_non_product_url(url, non_product):
    assert shop.is_non_product_url(url) is non_product


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [("39000", "KRW", "₩39,000"), ("28.5", "USD", "$28.50"), ("1,234.5", "eur", "€1,234.50"),
     ("4400", "JPY", "¥4,400"), ("12", "XYZ", "12.00 XYZ"), ("12", None, "12.00"), ("0", "USD", None),
     ("abc", "USD", None)],
)
def test_format_price(amount, currency, expected):
    assert shop.format_price(amount, currency) == expected


@pytest.mark.parametrize(
    ("url", "searched", "expected"),
    [
        ("https://www.etsy.com/listing/1/x", "global", "indie"),
        ("https://shop.redbubble.com/people/a/works/1", "global", "indie"),
        ("https://www.zazzle.co.uk/tee-123", "global", "indie"),
        ("https://www.uniqlo.com/us/en/products/E1/00", "global", "global"),
        ("https://www.idus.com/v2/product/123", "local", "local"),  # local makers stay local
        ("https://www.etsy.com/listing/1/x", "indie", "indie"),
        ("https://notetsy.com/p/1", "global", "global"),
    ],
)
def test_origin_for(url, searched, expected):
    assert shop.origin_for(url, searched) == expected


def test_site_of():
    assert shop.site_of("https://shop.brand.co.kr/p/1") == "brand.co.kr"
    assert shop.site_of("https://www.etsy.com/listing/1") == "etsy.com"
    assert shop.site_of("https://a.b.example/x") == "b.example"


# --------------------------------------------------------------------------- SSRF guard


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/products/x",
        "http://localhost/products/x",
        "http://printer.local/p/1",
        "http://10.0.0.5/p/1",
        "http://192.168.1.1/p/1",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/p/1",
        "http://[::ffff:127.0.0.1]/p/1",
        "http://0.0.0.0/p/1",
        "http://100.64.0.1/p/1",
        "http://evil.example/p/1",  # resolves to 10.0.0.7
        "http://rebind.example/p/1",  # one public + one loopback answer
        "http://metadata.example/p/1",
        "http://unknown-host.example/p/1",  # does not resolve
        "file:///c:/windows/win.ini",
        "ftp://shop.example/p/1",
        "gopher://shop.example/p/1",
        "http://user:pass@shop.example/p/1",
        "http://shop.example:22/p/1",
        "http://shop.example:8080/p/1",
    ],
)
async def test_check_url_blocks(url, monkeypatch):
    monkeypatch.setattr(shop, "_resolve_ips", fake_resolve)
    with pytest.raises(shop.BlockedURL):
        await shop._check_url(url)


async def test_check_url_allows_public(monkeypatch):
    monkeypatch.setattr(shop, "_resolve_ips", fake_resolve)
    await shop._check_url("https://shop.example/products/x")
    await shop._check_url("http://shop.example:80/products/x")
    await shop._check_url("https://93.184.216.34/p/1")


class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connected: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append((host, port))
        return object()

    async def sleep(self, seconds):  # pragma: no cover
        pass


async def test_guarded_backend_pins_vetted_ip_and_blocks_private(monkeypatch):
    monkeypatch.setattr(shop, "_resolve_ips", fake_resolve)
    inner = RecordingBackend()
    guard = shop._GuardedBackend(inner)
    await guard.connect_tcp("shop.example", 443)
    assert inner.connected == [("93.184.216.11", 443)]  # connects to the vetted IP, not the name
    for host in ("127.0.0.1", "evil.example", "rebind.example", "localhost", "metadata.example"):
        with pytest.raises(httpcore.ConnectError):
            await guard.connect_tcp(host, 80)
    assert len(inner.connected) == 1
    with pytest.raises(httpcore.ConnectError):
        await guard.connect_unix_socket("/var/run/docker.sock")


async def test_real_transport_has_guard_installed(monkeypatch):
    # conftest disables the real transport; re-enable it so the request reaches our guard, which
    # must refuse before any socket is opened.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _REAL_HANDLE_ASYNC)
    transport = shop._make_transport()
    assert isinstance(transport._pool._network_backend, shop._GuardedBackend)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(httpx.ConnectError, match="blocked"):
            await http.get("http://127.0.0.1:9/")
        with pytest.raises(httpx.ConnectError, match="blocked"):
            await http.get("http://localhost:9/")


@pytest.mark.parametrize(
    ("status", "headers", "walled"),
    [
        (403, {"server": "cloudflare"}, True),
        (503, {"server": "cloudflare"}, True),
        (403, {"server": "DataDome", "x-datadome": "protected"}, True),
        (403, {"server": "AkamaiGHost"}, True),
        (403, {"x-amzn-waf-action": "captcha"}, True),
        (429, {}, True),
        (403, {"server": "nginx"}, False),
        (404, {"server": "cloudflare"}, False),
        (410, {"x-datadome": "protected"}, False),
        (500, {"server": "cloudflare"}, False),
    ],
)
def test_bot_wall_detection(status, headers, walled):
    assert shop._is_bot_wall(httpx.Response(status, headers=headers)) is walled


def test_page_info_challenge_vs_soft_404():
    challenge = shop._page_info(shop._Page(url="https://x.example/p/1", html=page(title="Just a moment...", og_type=None)))
    assert challenge.walled and not challenge.missing
    gone = shop._page_info(shop._Page(url="https://x.example/p/1", html=page(title="Page not found", og_type=None)))
    assert gone.missing and not gone.walled
    product_404_name = shop._page_info(shop._Page(url="https://x.example/p/1", html=page(title="Error 404 Tee")))
    assert not product_404_name.missing  # real product data beats a scary title


def test_page_info_sold_out_requires_every_variant_out():
    group = {"@type": "ProductGroup", "name": "Tee", "hasVariant": [
        {"@type": "Product", "offers": {"availability": "https://schema.org/OutOfStock"}},
        {"@type": "Product", "offers": {"availability": "https://schema.org/InStock"}},
    ]}
    info = shop._page_info(shop._Page(url="https://x.example/p/1", html=page(jsonld=group)))
    assert info.is_product and not info.sold_out
    meta_oos = page(title="Tee").replace("</head>", '<meta property="product:availability" content="oos"></head>')
    assert shop._page_info(shop._Page(url="https://x.example/p/1", html=meta_oos)).sold_out


async def test_fetch_page_redirect_limit(monkeypatch):
    def loop_handler(request):
        n = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(302, headers={"location": f"/p/{n + 1}"})

    monkeypatch.setattr(shop, "_resolve_ips", fake_resolve)
    async with httpx.AsyncClient(transport=httpx.MockTransport(loop_handler)) as http:
        assert await shop._fetch_page(http, "https://shop.example/p/1") is None


def test_search_prompt_lists_balance_game_picks_and_requires_them():
    from types import SimpleNamespace as NS

    def opt(label, sub=None):
        return NS(label=label, sublabel=sub)

    answers = [
        NS(question=NS(option_a=opt("Slam Dunk", "スラムダンク"), option_b=opt("Dragon Ball")), choice="A"),
        NS(question=NS(option_a=opt("Shoyu ramen"), option_b=opt("Tonkotsu ramen")), choice="B"),
    ]
    picks = shop.picks_from_answers(answers)
    assert picks == ("Slam Dunk (スラムダンク) over Dragon Ball", "Tonkotsu ramen over Shoyu ramen")
    prompt = shop._search_prompt("global", make_profile(), Country(code="JP", name="Japan"), picks=picks)
    assert "1. Slam Dunk (スラムダンク) over Dragon Ball" in prompt and 'MUST start with "You picked' in prompt
    assert "THEIR BALANCE-GAME PICKS" not in shop._search_prompt("global", make_profile(), Country(code="JP", name="Japan"))



def test_pick_match_uses_only_the_chosen_option_and_handles_native_script():
    picks = ("Slam Dunk (スラムダンク) over Dragon Ball",)
    assert shop._match_selected_pick("Slam Dunk Graphic Tee", picks) == "Slam Dunk"
    assert shop._match_selected_pick("スラムダンク Tシャツ", picks) == "Slam Dunk"
    assert shop._match_selected_pick("Dragon Ball Tee", picks) is None


def test_product_pick_match_distinguishes_page_evidence_from_search_claim():
    picks = ("Slam Dunk over Dragon Ball",)
    raw = shop.RawProduct(url="https://store.example/p/slam-dunk-tee", title="Slam Dunk Tee", why="You picked Slam Dunk.")
    page = shop._PageInfo(title="Slam Dunk Tee", description="Official graphic tee")
    product = shop._build_product(raw, page, raw.url, "global", None, picks)
    assert product.pick_match == "Slam Dunk" and product.pick_match_source == "product_page"

    hidden_page = shop._PageInfo(walled=True)
    search_only = shop._build_product(raw, hidden_page, raw.url, "indie", None, picks)
    assert search_only.pick_match == "Slam Dunk" and search_only.pick_match_source == "search_result"
