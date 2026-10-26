"""shop.py latency and hardening: early return past a hung origin, joined in-flight searches,
linear JSON-LD scanning, vetted page images, no SSL-context rebuild per search."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app import context, llm, shop
from tests.factories import seed_session
from tests.test_shop import HTML, PAGES, SEARCH_RESULTS, item, origin_of, page, photo, web  # noqa: F401  (fixture)


def test_a_hung_origin_no_longer_holds_back_finished_results(web, monkeypatch):  # noqa: F811
    rec = seed_session(done=True, profile=True)
    monkeypatch.setattr(shop, "TOTAL_BUDGET_S", 60.0)  # the old code waited for all of this
    monkeypatch.setattr(shop, "MIN_RESULTS", 3)
    monkeypatch.setattr(shop, "SHOP_GRACE_S", 0.3)
    hung = asyncio.Event()

    async def search(model, prompt, **kwargs):
        if origin_of(prompt) == "indie":
            hung.set()
            await asyncio.sleep(50)  # this search (and its backup) never answers
        return json.dumps(SEARCH_RESULTS[origin_of(prompt)])

    monkeypatch.setattr(llm, "web_search_text", search)

    async def timed():
        started = time.perf_counter()
        products = await shop.suggest(rec.id)
        return products, time.perf_counter() - started

    products, elapsed = asyncio.run(timed())
    assert hung.is_set() and elapsed < 5
    assert {"Night Market Tee", "UT Graphic Tee"} <= {p.title for p in products}  # local + global
    assert {p.origin for p in products} <= {"local", "global", "indie"}


def test_concurrent_suggest_calls_share_one_search(web, monkeypatch):  # noqa: F811
    rec = seed_session(done=True, profile=True)
    prompts: list[str] = []

    async def search(model, prompt, **kwargs):
        prompts.append(prompt)
        await asyncio.sleep(0.05)
        return json.dumps(SEARCH_RESULTS[origin_of(prompt)])

    monkeypatch.setattr(llm, "web_search_text", search)

    async def twice():
        return await asyncio.gather(shop.suggest(rec.id), shop.suggest(rec.id))

    first, second = asyncio.run(twice())
    assert first == second and len(prompts) == 3  # one search per origin, not six
    assert not shop._inflight  # cleaned up afterwards


_HOSTILE = {
    "unclosed-blocks": lambda: '<script type="application/ld+json">' * 30_000,  # ~1 MB
    "unclosed-tags": lambda: "<script " * 150_000,  # ~1.2 MB of never-closed tags
    "endless-attributes": lambda: '<script type="application/ld+json" ' + "a=b " * 300_000,
}


@pytest.mark.parametrize("shape", list(_HOSTILE))
def test_jsonld_scan_is_linear_on_hostile_pages(shape):
    hostile = _HOSTILE[shape]()
    started = time.perf_counter()
    assert list(shop._iter_jsonld(hostile[: shop.MAX_PAGE_BYTES])) == []
    assert time.perf_counter() - started < 1.0


def test_jsonld_scan_still_finds_real_blocks():
    blocks = "".join(
        f'<script type="application/ld+json">{json.dumps({"@type": "Thing", "n": i})}</script><p>x</p>'
        for i in range(20)
    )
    html = f"<html><head><SCRIPT TYPE='application/ld+json' id=x>{json.dumps({'@type': 'Product'})}</SCRIPT></head>{blocks}"
    nodes = list(shop._iter_jsonld(html))
    assert len(nodes) == 21 and nodes[0]["@type"] == "Product"


def test_page_images_pointing_at_private_addresses_are_dropped(web, monkeypatch):  # noqa: F811
    rec = seed_session(done=True, profile=True)
    lan = "https://indie.example/listing/101010/lan-tee"
    monkeypatch.setitem(PAGES, lan, httpx.Response(
        200, headers=HTML, text=page(title="LAN Tee", image="http://192.168.0.1/cgi-bin/reboot.cgi?now=1")))
    ok = "https://indie.example/listing/202020/ok-tee"
    monkeypatch.setitem(PAGES, ok, httpx.Response(200, headers=HTML, text=page(title="OK Tee", image="https://cdn.example/ok.jpg")))
    monkeypatch.setitem(PAGES, "https://cdn.example/ok.jpg", photo())

    async def search(model, prompt, **kwargs):
        if origin_of(prompt) == "indie":
            return json.dumps([item(lan, "LAN Tee"), item(ok, "OK Tee")])
        return json.dumps(SEARCH_RESULTS[origin_of(prompt)])

    monkeypatch.setattr(llm, "web_search_text", search)
    by_title = {p.title: p for p in asyncio.run(shop.suggest(rec.id))}
    assert by_title["LAN Tee"].image_url is None
    assert by_title["OK Tee"].image_url == "https://cdn.example/ok.jpg"


def test_real_transport_reuses_the_cached_ssl_context():
    context._ssl_context()  # main.lifespan warms it at startup
    started = time.perf_counter()
    for _ in range(3):
        shop._make_transport()
    assert time.perf_counter() - started < 0.1  # was ~0.2 s EACH, on the event loop
