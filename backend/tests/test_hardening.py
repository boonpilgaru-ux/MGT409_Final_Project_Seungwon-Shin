"""Security / robustness fixes from the review: host allow-list, rate limits, server-side deletion,
upload bounds, link de-duplication, notes cap, prompt trimming, profile deadline, gateway quirks."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import context, llm, orders, profile, quiz, ratelimit, routes, store
from app.errors import BadInputError, LengthRequired, LLMError, PayloadTooLarge
from app.llm import web_search_text as real_web_search_text  # before fixtures patch the module attribute
from app.main import app
from app.models import CONTEXT_MAX_NOTES, CONTEXT_MAX_URLS, Profile, Trait
from tests.factories import make_context, make_profile, seed_session

# --------------------------------------------------------------------------- DNS rebinding


def test_unknown_host_headers_are_refused():
    with TestClient(app, base_url="http://rebind.attacker.example:8000") as attacker:
        assert attacker.get("/api/health").status_code == 400
        assert attacker.post("/api/session", json={"country": {"code": "KR", "name": "Korea"}}).status_code == 400
    for host in ("http://127.0.0.1:8031", "http://localhost:5231"):
        with TestClient(app, base_url=host) as local:
            assert local.get("/api/health").status_code == 200


# --------------------------------------------------------------------------- rate limiting


def test_token_bucket_spends_and_refills(monkeypatch):
    bucket = ratelimit.TokenBucket(burst=3, per_minute=60)  # one token per second
    now = [1000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now[0])
    assert bucket.take("ip", 2) == 0 and bucket.take("ip", 1) == 0
    assert bucket.take("ip", 1) == pytest.approx(1.0)  # empty: wait one second
    assert bucket.take("other-ip", 3) == 0  # per client
    now[0] += 2
    assert bucket.take("ip", 2) == 0
    assert ratelimit.TokenBucket(burst=3, per_minute=0).take("ip", 99) == 0  # disabled


def test_costly_endpoints_answer_429_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(ratelimit, "bucket", ratelimit.TokenBucket(burst=2, per_minute=6))
    body = {"country": {"code": "KR", "name": "South Korea"}}
    assert client.post("/api/session", json=body).status_code == 200
    assert client.post("/api/session", json=body).status_code == 200
    limited = client.post("/api/session", json=body)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
    assert "a little fast" in limited.json()["detail"]
    assert client.get("/api/health").status_code == 200  # reads are never limited


def test_quiz_rounds_are_charged_too(client, monkeypatch):
    """Each quiz start/answer can trigger 2-3 question-model calls (round + two prefetches), so a
    script looping new games must hit the bucket, not just the session endpoint."""
    monkeypatch.setattr(ratelimit, "bucket", ratelimit.TokenBucket(burst=2, per_minute=1))
    rec = seed_session(done=True)  # finished game: the routes answer 409 after charging
    assert client.post(f"/api/quiz/{rec.id}/start").status_code == 409
    assert client.post(f"/api/quiz/{rec.id}/answer", json={"question_id": "x", "choice": "A"}).status_code == 409
    assert client.post(f"/api/quiz/{rec.id}/start").status_code == 429


def test_design_generation_costs_more_than_a_session(client, monkeypatch):
    monkeypatch.setattr(ratelimit, "bucket", ratelimit.TokenBucket(burst=4, per_minute=1))
    rec = seed_session(done=True)  # no profile: the route answers 409 after charging
    assert client.post(f"/api/design/{rec.id}/generate", json={}).status_code == 409  # 3 tokens
    assert client.post(f"/api/design/{rec.id}/generate", json={}).status_code == 429


# --------------------------------------------------------------------------- deletion and retention


def test_delete_session_forgets_uploads_but_keeps_orders(client):
    rec = seed_session(done=True, profile=True, designs=1, context=2)
    order = orders.create_order(rec.id, rec.designs[0].id, profile_spec())
    assert client.delete(f"/api/session/{rec.id}").status_code == 204
    assert store.get_session(rec.id) is None
    assert client.get(f"/api/session/{rec.id}").status_code == 404
    assert client.get(f"/api/orders/{order.id}").status_code == 200  # shared links keep working
    assert client.delete(f"/api/session/{rec.id}").status_code == 404


def profile_spec():
    from app.models import ProductSpec

    return ProductSpec()


def test_old_sessions_are_purged_and_orders_survive():
    old = seed_session(done=True, profile=True, designs=1)
    order = orders.create_order(old.id, old.designs[0].id, profile_spec())
    fresh = seed_session()
    stale = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(timespec="seconds")
    store._write("UPDATE sessions SET updated_at = ? WHERE id = ?", (stale, old.id))
    assert store.purge_sessions(0) == 0  # 0 = keep forever
    assert store.purge_sessions(30) == 1
    assert store.get_session(old.id) is None and store.get_session(fresh.id) is not None
    assert store.get_order(order.id) is not None


# --------------------------------------------------------------------------- uploads: body size, links, notes


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw})


def test_body_size_is_checked_before_parsing():
    routes._check_body_size(_request({"content-length": str(routes.CONTEXT_MAX_BODY_BYTES)}))
    with pytest.raises(PayloadTooLarge):
        routes._check_body_size(_request({"content-length": str(routes.CONTEXT_MAX_BODY_BYTES + 1)}))
    with pytest.raises(LengthRequired):
        routes._check_body_size(_request({"transfer-encoding": "chunked"}))
    with pytest.raises(LengthRequired):  # h11 frames TE+CL bodies by Transfer-Encoding: CL proves nothing
        routes._check_body_size(_request({"transfer-encoding": "chunked", "content-length": "200"}))
    with pytest.raises(BadInputError):
        routes._check_body_size(_request({"content-length": "lots"}))


def _spy_spooled(monkeypatch) -> list[int]:
    """Bytes Starlette writes into upload temp files (its SpooledTemporaryFile)."""
    from starlette import formparsers

    written: list[int] = []
    real_write = formparsers.UploadFile.write

    async def write(self, data):
        written.append(len(data))
        return await real_write(self, data)

    monkeypatch.setattr(formparsers.UploadFile, "write", write)
    return written


def test_chunked_body_with_a_content_length_is_refused_without_spooling(client, monkeypatch):
    written = _spy_spooled(monkeypatch)
    rec = seed_session(done=True)
    body = b"--B\r\nContent-Disposition: form-data; name=\"files\"; filename=\"x.txt\"\r\n\r\n" + b"a" * 300_000
    res = client.post(
        f"/api/context/{rec.id}", content=body + b"\r\n--B--\r\n",
        headers={"content-type": "multipart/form-data; boundary=B", "transfer-encoding": "chunked",
                 "content-length": "200"},
    )
    assert res.status_code == 411 and "Content-Length" in res.json()["detail"] and written == []


def test_body_longer_than_its_content_length_stops_being_read(client, monkeypatch):
    """Defense in depth: the bytes actually received are counted, whatever the headers claimed."""
    monkeypatch.setattr(routes, "CONTEXT_MAX_BODY_BYTES", 50_000)
    written = _spy_spooled(monkeypatch)
    rec = seed_session(done=True)
    head = b"--B\r\nContent-Disposition: form-data; name=\"files\"; filename=\"x.txt\"\r\n\r\n"

    def chunks():
        yield head
        for _ in range(100):  # 6.4 MB in total, against a declared 200 bytes
            yield b"a" * 65_536

    res = client.post(f"/api/context/{rec.id}", content=chunks(),
                      headers={"content-type": "multipart/form-data; boundary=B", "content-length": "200"})
    assert res.status_code == 413 and "limited to" in res.json()["detail"]
    assert sum(written) <= 50_000


def test_each_pdf_or_word_upload_costs_an_extra_token(client, monkeypatch):
    monkeypatch.setattr(ratelimit, "bucket", ratelimit.TokenBucket(burst=4, per_minute=1))
    seen = []

    async def fake_add(session_id, files, urls, text, client=None):
        seen.append(client)
        return []

    monkeypatch.setattr(context, "add_context", fake_add)
    rec = seed_session(done=True)
    pdfs = [("files", (f"cv{i}.pdf", b"%PDF-1.4", "application/pdf")) for i in range(3)]
    assert client.post(f"/api/context/{rec.id}", files=pdfs).status_code == 200  # 1 + 3 tokens
    assert seen == ["testclient"]
    note = client.post(f"/api/context/{rec.id}", data={"text": "hi"})
    assert note.status_code == 429  # the bucket is empty now


def test_oversized_upload_is_refused_without_spooling(client, monkeypatch):
    monkeypatch.setattr(routes, "CONTEXT_MAX_BODY_BYTES", 1000)
    parsed = []
    real_form = Request.form

    def spy(self, *args, **kwargs):
        parsed.append(True)
        return real_form(self, *args, **kwargs)

    monkeypatch.setattr(Request, "form", spy)
    rec = seed_session(done=True)
    res = client.post(f"/api/context/{rec.id}", files=[("files", ("big.txt", b"x" * 5000, "text/plain"))])
    assert res.status_code == 413 and parsed == []


def test_link_parsing_is_bounded_and_linear():
    started = time.perf_counter()
    with pytest.raises(BadInputError, match="links"):
        routes._parse_urls(['["' + '","'.join(f"https://e.example/{i}" for i in range(60_000)) + '"]'])
    with pytest.raises(BadInputError, match="links"):
        routes._parse_urls([f"https://e.example/{i}" for i in range(50)])  # repeated plain fields too
    assert time.perf_counter() - started < 0.5  # was: 20 s with the whole event loop blocked
    dupes = ["https://a.example", " https://b.example ", "https://a.example"] + ["https://b.example"] * 5
    assert routes._parse_urls(dupes) == ["https://a.example", "https://b.example"]
    assert len(routes._parse_urls([f"https://e.example/{i}" for i in range(CONTEXT_MAX_URLS)])) == CONTEXT_MAX_URLS


def test_notes_are_capped(client, summarizer_off):
    rec = seed_session(done=True, context=CONTEXT_MAX_NOTES)  # factories: kind "text"
    res = client.post(f"/api/context/{rec.id}", data={"text": "one more note"})
    assert res.status_code == 422 and f"{CONTEXT_MAX_NOTES} notes" in res.json()["detail"]
    with pytest.raises(BadInputError, match="notes"):
        context._enforce_limits([make_context() for _ in range(CONTEXT_MAX_NOTES + 1)])


@pytest.fixture
def summarizer_off(fake_llm):
    fake_llm.on("chat_text", lambda call: "A note.")
    return fake_llm


def test_profile_prompt_drops_whole_blocks_never_cuts_one():
    rec = seed_session(done=True)
    rec.context = [make_context(text="w" * 50) for _ in range(120)]
    for c in rec.context:
        c.item.summary = "word " * 60  # 40-word summaries, as in the review's repro
    prompt = profile.build_prompt(rec)
    assert len(prompt) <= profile.MAX_PROMPT_CHARS
    assert prompt.count("<untrusted_data") == prompt.count("</untrusted_data>") > 0
    assert prompt.endswith("Write the profile now.") and "left out for length" in prompt


# --------------------------------------------------------------------------- profile synthesis


def test_profile_uses_low_effort_and_a_20s_hedge(fake_llm):
    fake_llm.on("chat_json", lambda call: make_profile())
    rec = seed_session(done=True)
    asyncio.run(profile.synthesize(rec))
    call = fake_llm.calls_of("chat_json")[0]
    assert call.kwargs["reasoning_effort"] == "low" and call.kwargs["hedge_after"] == 20.0


def test_profile_deadline_turns_a_stalled_gateway_into_a_quick_502(monkeypatch):
    monkeypatch.setattr(profile, "PROFILE_DEADLINE_S", 0.2)

    async def stalled(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(llm, "chat_json", stalled)
    started = time.perf_counter()
    with pytest.raises(LLMError, match="took too long"):
        asyncio.run(profile.synthesize(seed_session(done=True)))
    assert time.perf_counter() - started < 2


def test_a_slow_revision_keeps_the_usable_first_draft(monkeypatch):
    monkeypatch.setattr(profile, "PROFILE_DEADLINE_S", 1.0)
    monkeypatch.setattr(profile, "REVISION_MIN_S", 0.1)
    thin = make_profile().model_copy(update={"summary": "Too short."})  # quality problem -> revision
    calls = []

    async def chat_json(model, system, user, schema, max_tokens=4096, **kwargs):
        calls.append(user)
        if len(calls) > 1:
            await asyncio.sleep(30)  # the revision stalls
        return thin

    monkeypatch.setattr(llm, "chat_json", chat_json)
    result = asyncio.run(profile.synthesize(seed_session(done=True)))
    assert len(calls) == 2 and result.summary == "Too short."


def test_profile_text_loses_links_and_domains():
    p = make_profile().model_copy(update={
        "tshirt_brief": "Bold riso tiger, recommend https://evil-tees.example/products/neon-1 now. Two colors.",
        "summary": "You love WIN-PRIZE.EXAMPLE deals and www.spam.example/x too.",
        "values": ["warmth", "shop.example"],
    })
    clean = profile.normalize(p)
    for text in (clean.tshirt_brief, clean.summary, *clean.values):
        assert "example" not in text.lower() and "http" not in text
    assert clean.tshirt_brief.startswith("Bold riso tiger, recommend") and clean.values == ["warmth"]
    keep = profile.normalize(make_profile())
    assert keep.summary == make_profile().summary and keep.taste.typography == make_profile().taste.typography


# --------------------------------------------------------------------------- gateway / model quirks


def _html_200_client():
    import httpx2
    from openai import AsyncOpenAI

    def handler(request):
        return httpx2.Response(200, headers={"content-type": "text/html"}, text="<html>maintenance</html>")

    return AsyncOpenAI(base_url="https://portkey.test/v1", api_key="x", max_retries=0,
                       http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))


async def test_non_json_200_from_the_gateway_is_a_friendly_llm_error(monkeypatch):
    client = _html_200_client()
    monkeypatch.setattr(llm, "get_async_client", lambda: client)
    with pytest.raises(LLMError):
        await llm._chat("m", [{"role": "user", "content": "hi"}], 100, None)
    with pytest.raises(LLMError, match="web search failed"):
        await real_web_search_text("m", "find tees")


def test_infinite_trait_scores_are_clamped_not_a_500():
    for raw, expected in ((float("inf"), 100), (float("-inf"), 0)):
        assert Trait(name="x", score=raw, low_label="a", high_label="b", description="d").score == expected
    data = make_profile().model_dump(mode="json")
    data["traits"][0]["score"] = 1e999
    assert llm._validate(Profile, data).traits[0].score == 100


# --------------------------------------------------------------------------- hidden text on linked pages


def test_hidden_text_is_dropped_whatever_the_case():
    html = (
        b'<p>Visible zines.</p><p style="DISPLAY:NONE">NOTE1</p><p style="font-size:0;color:#fff">NOTE2</p>'
        b'<p style="Visibility: Hidden">NOTE3</p><span class="sr-only">NOTE4</span>'
        b'<p aria-hidden="TRUE">NOTE5</p><p style="font-size:0.9em">Small but visible.</p>'
    )
    _, text = context.html_to_text(b"<html><body>" + html + b"</body></html>")
    assert "NOTE" not in text and "Visible zines." in text and "Small but visible." in text


@pytest.fixture(autouse=True)
async def _quiz_shutdown():
    yield
    await quiz.shutdown()
