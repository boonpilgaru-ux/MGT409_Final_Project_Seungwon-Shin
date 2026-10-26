"""shop.py product photos: candidate extraction, the browser-faithful image check (no Referer, CORP,
real raster size, SSRF-safe redirects), photo-first balancing, and late hedged search answers.

Network is simulated as in test_shop.py (fake DNS + MockTransport)."""

from __future__ import annotations

import asyncio
import json
import struct
import time

import httpx
import pytest

from app import llm, shop
from app.models import Product
from tests.factories import seed_session
from tests.test_shop import HTML, PAGES, SEARCH_RESULTS, fake_resolve, item, origin_of, page, photo, png, web  # noqa: F401


def _client() -> httpx.AsyncClient:
    return shop._make_client()  # the web fixture swapped in the fake transport + DNS


def _jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, height, width) + b"\x03" + b"\x01\x22\x00" * 3
    return b"\xff\xd8" + app0 + sof0 + b"\0" * 64


def _webp(chunk: bytes, payload: bytes) -> bytes:
    return b"RIFF" + struct.pack("<I", 100) + b"WEBP" + chunk + struct.pack("<I", len(payload)) + payload


# --------------------------------------------------------------------------- image_size


def test_image_size_reads_common_headers():
    assert shop.image_size(png(640, 480)) == (640, 480)
    assert shop.image_size(b"GIF89a" + struct.pack("<HH", 320, 200) + b"\0" * 8) == (320, 200)
    assert shop.image_size(_jpeg(1200, 900)) == (1200, 900)
    vp8 = b"\x9d\x01\x2a" + struct.pack("<HH", 500, 400)
    assert shop.image_size(_webp(b"VP8 ", b"\0\0\0" + vp8 + b"\0" * 8)) == (500, 400)
    bits = (300 - 1) | ((200 - 1) << 14)
    assert shop.image_size(_webp(b"VP8L", b"\x2f" + bits.to_bytes(4, "little") + b"\0" * 8)) == (300, 200)
    vp8x = b"\0" * 4 + (799).to_bytes(3, "little") + (599).to_bytes(3, "little")
    assert shop.image_size(_webp(b"VP8X", vp8x + b"\0" * 8)) == (800, 600)
    assert shop.image_size(b"<html>not an image</html>") is None
    assert shop.image_size(b"\xff\xd8\xff") is None  # truncated JPEG


# --------------------------------------------------------------------------- _usable_image


async def _usable(url: str) -> str | None:
    async with _client() as http:
        return await shop._usable_image(http, url)


async def test_usable_image_is_checked_like_the_browser_loads_it(web, monkeypatch):  # noqa: F811
    monkeypatch.setitem(PAGES, "https://cdn.example/a.jpg", photo())
    assert await _usable("https://cdn.example/a.jpg") == "https://cdn.example/a.jpg"
    [request] = [r for r in web.requests if str(r.url) == "https://cdn.example/a.jpg"]
    assert "referer" not in request.headers  # same as <img referrerpolicy="no-referrer">: hotlink rules agree
    assert request.headers["accept"].startswith("image/") and request.headers["user-agent"] == shop.USER_AGENT


@pytest.mark.parametrize(
    ("response", "usable"),
    [
        (photo(), True),
        (photo(**{"cross-origin-resource-policy": "cross-origin"}), True),
        (photo(**{"cross-origin-resource-policy": "same-origin"}), False),  # the browser would refuse it
        (photo(**{"cross-origin-resource-policy": "same-site"}), False),
        (photo(1, 1), False),  # tracking pixel
        (photo(64, 64), False),  # icon
        (httpx.Response(200, headers={"content-type": "image/svg+xml"}, content=b"<svg/>" + b" " * 2000), False),
        (httpx.Response(200, headers=HTML, content=b"<html>login</html>" + b" " * 2000), False),
        (httpx.Response(403, headers={"content-type": "image/jpeg"}, content=_jpeg(800, 800)), False),
        (httpx.Response(200, headers={"content-type": "image/avif"}, content=b"\0\0\0\x1cftypavif" + b"\1" * 3000), True),
        (httpx.Response(200, headers={"content-type": "image/avif"}, content=b"\0\0\0\x1cftypavif"), False),
    ],
)
async def test_usable_image_rules(web, monkeypatch, response, usable):  # noqa: F811
    monkeypatch.setitem(PAGES, "https://cdn.example/x.img", response)
    assert (await _usable("https://cdn.example/x.img") is not None) is usable


async def test_usable_image_follows_public_redirects_and_returns_the_final_url(web, monkeypatch):  # noqa: F811
    monkeypatch.setitem(PAGES, "http://img.example/old.jpg", httpx.Response(301, headers={"location": "https://img.example/new.jpg"}))
    monkeypatch.setitem(PAGES, "https://img.example/new.jpg", photo())
    assert await _usable("http://img.example/old.jpg") == "https://img.example/new.jpg"  # browsers skip the hop


async def test_usable_image_never_touches_private_addresses(web, monkeypatch):  # noqa: F811
    monkeypatch.setitem(PAGES, "https://img.example/hop.jpg", httpx.Response(302, headers={"location": "http://evil.example/cam.jpg"}))
    assert await _usable("https://img.example/hop.jpg") is None
    assert await _usable("http://192.168.0.1/cgi-bin/reboot.cgi?now=1") is None
    assert await _usable("http://metadata.example/latest/meta-data") is None
    assert not any(h in u for u in web.requested for h in ("evil.example", "192.168", "metadata.example"))


async def test_find_image_falls_back_through_candidates(web, monkeypatch):  # noqa: F811
    monkeypatch.setitem(PAGES, "https://kr.brand.example/img/second.jpg", photo())
    async with _client() as http:
        run = shop._Run(http=http, deadline=time.monotonic() + 30, semaphore=asyncio.Semaphore(4))
        sources = ["/img/missing.jpg", "data:image/png;base64,xx", None, "/img/second.jpg", "https://cdn.example/never.jpg"]
        image = await shop._find_image(run, sources, "https://kr.brand.example/products/x")
    assert image == "https://kr.brand.example/img/second.jpg"
    assert "https://cdn.example/never.jpg" not in web.requested  # stops at the first usable photo


async def test_a_hanging_image_server_costs_the_photo_not_the_product(web, monkeypatch):  # noqa: F811
    monkeypatch.setattr(shop, "IMAGE_BUDGET_S", 0.3)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "slow.example":
            await asyncio.sleep(10)
        return web.handler(request)

    monkeypatch.setattr(shop, "_make_transport", lambda: httpx.MockTransport(handler))
    monkeypatch.setitem(shop_dns(), "slow.example", "93.184.216.20")
    lazy = "https://indie.example/listing/303030/slow-photo-tee"
    monkeypatch.setitem(PAGES, lazy, httpx.Response(200, headers=HTML, text=page(title="Slow Photo Tee", image="https://slow.example/p.jpg")))
    raw = shop.RawProduct(url=lazy, title="Slow Photo Tee")
    async with _client() as http:
        run = shop._Run(http=http, deadline=time.monotonic() + 30, semaphore=asyncio.Semaphore(4))
        started = time.monotonic()
        found = await shop._validate(run, raw, lazy, "indie")
    assert found is not None and found.product.image_url is None and time.monotonic() - started < 2


async def test_a_product_link_that_bounces_to_a_listing_is_dropped(web, monkeypatch):  # noqa: F811
    gone = "https://global.example/us/carhartt-wip-punched-t-shirt-i036006-d6xx.html"
    listing = "https://global.example/en-us/collections/mens-carhartt-wip"
    monkeypatch.setitem(PAGES, gone, httpx.Response(301, headers={"location": listing}))
    monkeypatch.setitem(PAGES, listing, httpx.Response(200, headers=HTML, text=page(
        title="Carhartt WIP", og_type=None, image="https://img.example/ut.jpg",
        jsonld={"@type": "Product", "name": "Some other tee"})))  # listings often embed Product data
    async with _client() as http:
        run = shop._Run(http=http, deadline=time.monotonic() + 30, semaphore=asyncio.Semaphore(4))
        assert await shop._validate(run, shop.RawProduct(url=gone, title="Punched Tee"), gone, "global") is None


def shop_dns() -> dict:
    from tests import test_shop

    return test_shop.DNS


# --------------------------------------------------------------------------- page photo candidates


def test_page_info_collects_photo_candidates_in_order():
    ld = {"@type": "Product", "name": "Tee", "image": [{"@type": "ImageObject", "url": "/ld-1.jpg"}, "/ld-2.jpg", "/ld-3.jpg"]}
    html = page(title="Tee", image="/og.jpg", twitter="/og.jpg", jsonld=ld).replace(
        "</head>", '<meta itemprop="image" content="/itemprop.jpg"></head>')
    info = shop._page_info(shop._Page(url="https://x.example/p/1", html=html))
    assert info.images == ["/og.jpg", "/itemprop.jpg", "/ld-1.jpg", "/ld-2.jpg"]  # deduped, JSON-LD capped at 2


def test_page_info_spots_challenge_pages_that_put_the_title_in_the_body():
    hang_tight = ('<html><head><meta charset="utf-8"><style></style></head><body onload="">'
                  "<title>Hang Tight! Routing to checkout...</title><p>spinner</p></body></html>")
    assert shop._page_info(shop._Page(url="https://x.example/p/1", html=hang_tight)).walled
    icons = '<html><head></head><body><svg><title>Close</title></svg><p>Tee</p></body></html>'
    info = shop._page_info(shop._Page(url="https://x.example/p/1", html=icons))
    assert not info.walled and not info.missing and info.title is None  # an SVG <title> names nothing


def test_suggest_uses_the_next_page_photo_when_og_image_is_broken(fake_llm, web, monkeypatch):  # noqa: F811
    rec = seed_session(done=True, profile=True)
    url = "https://indie.example/listing/606060/ld-photo-tee"
    ld = {"@type": "Product", "name": "LD Photo Tee", "image": ["https://img.example/ld.jpg"]}
    monkeypatch.setitem(PAGES, url, httpx.Response(200, headers=HTML, text=page(
        title="LD Photo Tee", image="https://cdn.example/gone.jpg", jsonld=ld)))
    monkeypatch.setitem(PAGES, "https://img.example/ld.jpg", photo())
    fake_llm.on("web_search", lambda call: json.dumps(
        [item(url, "LD Photo Tee")] if origin_of(call.user) == "indie" else SEARCH_RESULTS[origin_of(call.user)]))
    products = asyncio.run(shop.suggest(rec.id))
    assert {p.title: p.image_url for p in products}["LD Photo Tee"] == "https://img.example/ld.jpg"


# --------------------------------------------------------------------------- balancing with photos


def _p(i: int, origin: str, *, store: str = "", image: str | None = "own", tier: int = shop.TIER_PRODUCT_PAGE):
    store = store or f"s{origin}{i}.example"
    image = f"https://{store}/img/{origin}{i}.jpg" if image == "own" else image
    product = Product(id=f"{origin}{i}", title=f"T{origin}{i}", brand="B", retailer="R",
                      url=f"https://{store}/p/{origin}{i}", image_url=image, origin=origin, why="w")
    return shop._Found(product=product, tier=tier)


def test_balance_puts_photos_first_and_caps_the_photoless_share():
    found = {
        "local": [_p(i, "local", image=None, tier=shop.TIER_UNVERIFIED) for i in range(4)] + [_p(9, "local")],
        "global": [_p(i, "global") for i in range(4)],
        "indie": [_p(i, "indie", image=None, tier=shop.TIER_UNVERIFIED) for i in range(4)] + [_p(i, "indie") for i in (7, 8, 9)],
    }
    picked = shop.balance(found)
    with_photo = [p for p in picked if p.image_url]
    assert len(with_photo) == 8  # every photographed shirt
    assert len(picked) - len(with_photo) == 2  # 8 // 3: at most a quarter of the list without a photo
    assert len(picked) / len(with_photo) <= 1 / (1 - shop.MAX_PHOTOLESS_SHARE)
    assert [p.origin for p in picked[:3]] == ["local", "global", "indie"]  # still interleaved
    local = [p for p in picked if p.origin == "local"]
    assert local[0].image_url and not local[-1].image_url  # within an origin: photo first


def test_balance_fills_up_to_the_minimum_and_keeps_every_origin():
    found = {
        "local": [_p(i, "local", image=None, tier=shop.TIER_UNVERIFIED) for i in range(6)],  # all bot-walled
        "global": [_p(i, "global") for i in range(2)],
        "indie": [_p(i, "indie") for i in range(2)] + [_p(5, "indie", image=None)],
    }
    picked = shop.balance(found)
    assert len(picked) == shop.MIN_RESULTS  # 4 with photos + 4 without, to reach the minimum
    assert sum(1 for p in picked if p.origin == "local") >= 1  # the origin with no photo is still shown
    many = {"local": [_p(i, "local", image=None) for i in range(3)],
            "global": [_p(i, "global") for i in range(6)], "indie": [_p(i, "indie") for i in range(6)]}
    picked = shop.balance(many)
    assert [p.origin for p in picked].count("local") == 1  # coverage: its best one only
    assert len(picked) == shop.MAX_RESULTS


def test_balance_treats_a_shared_image_as_a_logo():
    logo = "https://store.example/logo.png"
    found = {"local": [_p(1, "local", store="store.example", image=logo), _p(2, "local", store="store.example", image=logo),
                       _p(3, "local")], "global": [], "indie": []}
    by_id = {p.id: p for p in shop.balance(found)}
    assert by_id["local1"].image_url is None and by_id["local2"].image_url is None
    assert by_id["local3"].image_url


def test_balance_counts_each_maker_subdomain_as_its_own_store():
    found = {
        "local": [_p(i, "local") for i in range(4)],
        "global": [_p(i, "global") for i in range(4)],
        "indie": [_p(i, "indie", store=f"artist{i}.bandcamp.com") for i in range(4)],
    }
    picked = shop.balance(found)
    assert sum(1 for p in picked if "bandcamp.com" in p.url) == 4 and len(picked) == 12
    assert shop.store_of("https://artist1.bandcamp.com/merch/tee") == "artist1.bandcamp.com"
    assert shop.store_of("https://www.etsy.com/listing/1/x") == "etsy.com"


# --------------------------------------------------------------------------- late hedged answers


LATE_URL = "https://indie.example/listing/707070/late-tee"


def _late_search(monkeypatch, delay: float):
    """Primary global search hangs ``delay`` s and then answers with one extra shirt; the backup
    (hedged after 0.1 s) answers at once with the usual results."""
    monkeypatch.setattr(shop, "HEDGE_AFTER_S", 0.1)
    monkeypatch.setitem(PAGES, LATE_URL, httpx.Response(200, headers=HTML, text=page(title="Late Tee", image="https://img.example/late.jpg")))
    monkeypatch.setitem(PAGES, "https://img.example/late.jpg", photo())
    calls: list[str] = []

    async def search(model, prompt, **kwargs):
        origin = origin_of(prompt)
        calls.append(origin)
        if origin == "global" and calls.count("global") == 1:
            await asyncio.sleep(delay)
            return json.dumps([item(LATE_URL, "Late Tee")])
        return json.dumps(SEARCH_RESULTS[origin])

    monkeypatch.setattr(llm, "web_search_text", search)


def test_a_late_hedged_answer_adds_shirts_when_photos_are_short(web, monkeypatch):  # noqa: F811
    rec = seed_session(done=True, profile=True)
    _late_search(monkeypatch, delay=0.5)
    monkeypatch.setattr(shop, "MIN_RESULTS", 10)  # 6 photographed shirts without the late answer
    started = time.monotonic()
    products = asyncio.run(shop.suggest(rec.id))
    assert "Late Tee" in {p.title for p in products} and time.monotonic() - started < 3


def test_a_late_hedged_answer_is_not_waited_for_when_photos_suffice(web, monkeypatch):  # noqa: F811
    rec = seed_session(done=True, profile=True)
    _late_search(monkeypatch, delay=20)
    monkeypatch.setattr(shop, "MIN_RESULTS", 4)
    started = time.monotonic()
    products = asyncio.run(shop.suggest(rec.id))
    assert "Late Tee" not in {p.title for p in products} and time.monotonic() - started < 3
    assert "UT Graphic Tee" in {p.title for p in products}  # the backup's answer was used


@pytest.mark.parametrize(("delay", "included"), [(0.4, True), (1.6, False)])
def test_the_grace_period_waits_for_late_answers_and_the_slow_origin_together(web, monkeypatch, delay, included):  # noqa: F811
    rec = seed_session(done=True, profile=True)
    monkeypatch.setattr(shop, "HEDGE_AFTER_S", 0.1)
    monkeypatch.setattr(shop, "SHOP_GRACE_S", 1.0)
    monkeypatch.setitem(PAGES, LATE_URL, httpx.Response(200, headers=HTML, text=page(title="Late Tee", image="https://img.example/late.jpg")))
    monkeypatch.setitem(PAGES, "https://img.example/late.jpg", photo())
    calls: list[str] = []

    async def search(model, prompt, **kwargs):
        origin = origin_of(prompt)
        calls.append(origin)
        if origin == "indie":
            await asyncio.sleep(50)  # never answers
        if origin == "global" and calls.count("global") == 1:
            await asyncio.sleep(delay)
            return json.dumps([item(LATE_URL, "Late Tee")])
        return json.dumps(SEARCH_RESULTS[origin])

    monkeypatch.setattr(llm, "web_search_text", search)
    # 6 verified from local + global starts the grace for indie; only 4 of them have a photo.
    monkeypatch.setattr(shop, "MIN_RESULTS", 5)
    started = time.monotonic()
    products = asyncio.run(shop.suggest(rec.id))
    elapsed = time.monotonic() - started
    assert ("Late Tee" in {p.title for p in products}) is included
    assert elapsed < 1.5  # one shared ~1 s grace, not the slow origin's and then the late answer's


def test_a_search_answering_within_the_grace_gets_to_check_its_links(web, monkeypatch):  # noqa: F811
    rec = seed_session(done=True, profile=True)
    monkeypatch.setattr(shop, "SHOP_GRACE_S", 0.5)
    monkeypatch.setattr(shop, "LINK_CHECK_GRACE_S", 3.0)
    monkeypatch.setattr(shop, "MIN_RESULTS", 5)  # local + global verify 6 at once: indie gets the grace

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "indie.example":
            await asyncio.sleep(0.6)  # its pages are slow: checking ends after the grace
        return web.handler(request)

    async def search(model, prompt, **kwargs):
        if origin_of(prompt) == "indie":
            await asyncio.sleep(0.3)  # answers within the grace
        return json.dumps(SEARCH_RESULTS[origin_of(prompt)])

    monkeypatch.setattr(shop, "_make_transport", lambda: httpx.MockTransport(handler))
    monkeypatch.setattr(llm, "web_search_text", search)
    started = time.monotonic()
    titles = {p.title for p in asyncio.run(shop.suggest(rec.id))}
    assert {"Riso Tiger Tee", "Plain Tee"} <= titles and time.monotonic() - started < 3


def test_search_prompt_asks_for_store_variety():
    from tests.factories import KOREA, make_profile

    prompt = shop._search_prompt("indie", make_profile(), KOREA)
    assert "at most 2 from the" in prompt and "same website or marketplace" in prompt
