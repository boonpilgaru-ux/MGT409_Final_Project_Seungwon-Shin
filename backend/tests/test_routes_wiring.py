"""Route -> feature-module wiring. Each feature function is monkeypatched with a recorder, so these
tests pin the exact call contract routes.py relies on, independent of the real implementations."""

from __future__ import annotations

import json

import httpx
import pytest

from app import context, design, orders, profile, quiz, shop
from app.models import Answer, AnswerResult, ContextItem, Order, Price, PriceLine, UploadedFile
from tests.factories import make_design, make_product, make_profile, make_question, seed_session


class Recorder:
    """Async/sync stand-in that records its arguments and returns a fixed value."""

    def __init__(self, result=None, *, is_async: bool = True):
        self.calls: list[tuple] = []
        self.kwargs: list[dict] = []
        self.result = result
        self.is_async = is_async

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        self.kwargs.append(kwargs)
        if not self.is_async:
            return self.result

        async def _run():
            return self.result

        return _run()


def _patch(monkeypatch, module, name, result=None, *, is_async=True) -> Recorder:
    rec = Recorder(result, is_async=is_async)
    monkeypatch.setattr(module, name, rec)
    return rec


def test_quiz_start_and_answer_wiring(client, monkeypatch):
    rec = seed_session(started=False)
    q1, q2 = make_question(1), make_question(2)
    start = _patch(monkeypatch, quiz, "start_quiz", q1)
    res = client.post(f"/api/quiz/{rec.id}/start")
    assert res.status_code == 200 and res.json()["id"] == q1.id
    assert start.calls == [(rec.id,)]

    rec = seed_session(answered=1)
    ans = _patch(monkeypatch, quiz, "answer", AnswerResult(next=q2, done=False))
    body = {"question_id": rec.current.question.id, "choice": "B"}
    res = client.post(f"/api/quiz/{rec.id}/answer", json=body)
    assert res.status_code == 200 and res.json() == {"next": q2.model_dump(mode="json"), "done": False}
    assert ans.calls == [(rec.id, Answer(**body))]


def test_quiz_answer_before_start_is_409(client):
    rec = seed_session(started=False)
    assert client.post(f"/api/quiz/{rec.id}/answer", json={"question_id": "x", "choice": "A"}).status_code == 409


def test_context_wiring_passes_files_urls_text(client, monkeypatch):
    rec = seed_session()
    item = ContextItem(id="c1", kind="text", name="Notes", chars=5, status="ok", summary="hi", error=None)
    add = _patch(monkeypatch, context, "add_context", [item])
    res = client.post(
        f"/api/context/{rec.id}",
        files=[("files", ("../../essay.md", b"# me", "text/markdown"))],
        data={"urls": json.dumps(["https://example.com/me", "https://example.com/me"]), "text": "  hello  "},
    )
    assert res.status_code == 200 and res.json() == {"items": [item.model_dump(mode="json")]}
    (sid, files, urls, text), = add.calls
    assert sid == rec.id
    assert files == [UploadedFile("essay.md", b"# me", "text/markdown")]  # path stripped from filename
    assert urls == ["https://example.com/me"]  # de-duplicated
    assert text == "hello"
    assert add.kwargs == [{"client": "testclient"}]  # PDF/DOCX parses are queued per client

    remove = _patch(monkeypatch, context, "remove_context", [])
    seeded = seed_session(context=1)
    item_id = seeded.context[0].item.id
    res = client.delete(f"/api/context/{seeded.id}/{item_id}")
    assert res.status_code == 200 and res.json() == {"items": []}
    assert remove.calls == [(seeded.id, item_id)]


def test_context_rejects_oversized_text(client):
    rec = seed_session()
    assert client.post(f"/api/context/{rec.id}", data={"text": "x" * 20_001}).status_code == 422


def test_profile_shop_design_wiring(client, monkeypatch):
    rec = seed_session(done=True, profile=True, designs=1)
    prof = _patch(monkeypatch, profile, "build_profile", make_profile())
    assert client.post(f"/api/profile/{rec.id}").json()["archetype"] == make_profile().archetype
    assert prof.calls == [(rec.id,)]

    sug = _patch(monkeypatch, shop, "suggest", [make_product(1, "local")])
    res = client.post(f"/api/shop/{rec.id}/suggest")
    assert res.status_code == 200 and res.json()["products"][0]["origin"] == "local"
    assert sug.calls == [(rec.id,)]

    new = make_design()
    gen = _patch(monkeypatch, design, "generate", [new])
    res = client.post(f"/api/design/{rec.id}/generate")  # no body at all -> defaults
    assert res.status_code == 200 and res.json()["designs"][0]["id"] == new.id
    base = rec.designs[0].id
    client.post(f"/api/design/{rec.id}/generate", json={"feedback": "", "base_design_id": base, "count": 1})
    client.post(f"/api/design/{rec.id}/generate", json={"feedback": "bolder", "base_design_id": None, "count": 2})
    assert gen.calls == [(rec.id, None, None, 3), (rec.id, None, base, 1), (rec.id, "bolder", None, 2)]


def _order(rec, status="draft", customer=None) -> Order:
    price = Price(unit=20.0, total=20.0, breakdown=[PriceLine(label="Cotton tee", amount=20.0)])
    d = rec.designs[0]
    return Order(id="ord1", created_at="2026-10-14T00:00:00+00:00", status=status, design=d,
                 spec=d.recommended_spec, price=price, profile_archetype=None, country=rec.country,
                 customer=customer)


def test_order_routes_wiring(client, monkeypatch):
    rec = seed_session(done=True, profile=True, designs=1)
    order = _order(rec)
    create = _patch(monkeypatch, orders, "create_order", order, is_async=False)
    spec = {"shirt_color": "#000", "fit": "boxy", "quantity": 12}
    res = client.post("/api/orders", json={"session_id": rec.id, "design_id": rec.designs[0].id, "spec": spec})
    assert res.status_code == 200 and res.json()["id"] == "ord1"
    sid, did, got_spec = create.calls[0]
    assert (sid, did) == (rec.id, rec.designs[0].id)
    assert got_spec.shirt_color == "#000000" and got_spec.fit == "boxy" and got_spec.material == "cotton"

    _patch(monkeypatch, orders, "get_order", order, is_async=False)
    assert client.get("/api/orders/ord1").json()["status"] == "draft"

    customer = {"name": "Mina", "email": "mina@example.com", "address": "1 Main St"}
    requested = _order(rec, "requested", customer)
    req = _patch(monkeypatch, orders, "request_order", (requested, "tok123"), is_async=False)
    res = client.post("/api/orders/ord1/request", json=customer)
    assert res.status_code == 200 and res.json()["status"] == "requested"
    assert res.json()["customer"] == customer  # the requester sees what they typed
    assert res.headers["x-order-token"] == "tok123"
    assert req.calls[0][0] == "ord1" and req.calls[0][1].email == "mina@example.com" and req.calls[0][2] is None
    client.post("/api/orders/ord1/request", json=customer, headers={"X-Order-Token": "tok123"})
    assert req.calls[1][2] == "tok123"
    bad = client.post("/api/orders/ord1/request", json={**customer, "email": "nope"})
    assert bad.status_code == 422 and "email" in bad.json()["detail"]

    _patch(monkeypatch, orders, "get_order", requested, is_async=False)
    public = client.get("/api/orders/ord1").json()["customer"]
    assert public == {"name": "Mina", "email": "m•••@example.com", "address": "(hidden)"}

    _patch(monkeypatch, orders, "print_svg", rec.designs[0].svg, is_async=False)
    res = client.get("/api/orders/ord1/print.svg")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/svg+xml")
    assert "attachment" in res.headers["content-disposition"]
    assert res.text.startswith("<svg")


def test_unknown_order_is_404(client, monkeypatch):
    _patch(monkeypatch, orders, "get_order", None, is_async=False)
    assert client.get("/api/orders/missing").status_code == 404
    assert client.get("/api/orders/missing/print.svg").status_code == 404
    customer = {"name": "A", "email": "a@b.co", "address": "x"}
    assert client.post("/api/orders/missing/request", json=customer).status_code == 404


def test_not_implemented_maps_to_501(client, monkeypatch):
    rec = seed_session(done=True)

    async def stub(session_id):
        raise NotImplementedError("profile.build_profile")

    monkeypatch.setattr(profile, "build_profile", stub)
    res = client.post(f"/api/profile/{rec.id}")
    assert res.status_code == 501 and "profile.build_profile" in res.json()["detail"]


def test_llm_error_maps_to_502(client, monkeypatch):
    from app.errors import LLMError

    rec = seed_session(done=True)

    async def boom(session_id):
        raise LLMError()

    monkeypatch.setattr(profile, "build_profile", boom)
    res = client.post(f"/api/profile/{rec.id}")
    assert res.status_code == 502 and res.json() == {"detail": LLMError.default_detail}


async def test_real_network_is_blocked_but_mock_transport_works():
    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.ConnectError):
            await http.get("https://example.com")
    mock = httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
    async with httpx.AsyncClient(transport=mock) as http:
        assert (await http.get("https://example.com")).text == "ok"
