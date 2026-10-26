"""Scaffold-level API tests: health, sessions, and route-level state checks (no feature module needed)."""

from __future__ import annotations

import json

from app import store
from tests.factories import seed_session


def test_health(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert isinstance(body["portkey_key_set"], bool)
    assert {"question", "profile", "shop", "design"} <= set(body["models"])


def test_create_and_get_session(client):
    res = client.post("/api/session", json={"country": {"code": "kr", "name": "South Korea"}})
    assert res.status_code == 200
    state = res.json()
    assert state["country"] == {"code": "KR", "name": "South Korea"}
    assert state["answers"] == [] and state["context"] == [] and state["designs"] == []
    assert state["current_question"] is None and state["profile"] is None and state["quiz_done"] is False
    assert len(state["id"]) == 12

    again = client.get(f"/api/session/{state['id']}")
    assert again.status_code == 200
    assert again.json() == state


def test_unknown_session_is_404_with_string_detail(client):
    res = client.get("/api/session/nope")
    assert res.status_code == 404
    assert isinstance(res.json()["detail"], str)


def test_validation_errors_have_string_detail(client):
    res = client.post("/api/session", json={"country": {"code": "KOR", "name": ""}})
    assert res.status_code == 422
    assert isinstance(res.json()["detail"], str) and "country" in res.json()["detail"]


def test_hidden_fields_not_exposed(client):
    rec = seed_session(answered=3, hypothesis="secret hypothesis", context=1)
    state = client.get(f"/api/session/{rec.id}").json()
    raw = json.dumps(state)
    assert "secret hypothesis" not in raw and "dimension" not in raw and "I shoot film" not in raw
    assert len(state["answers"]) == 3 and state["current_question"]["index"] == 4
    assert state["context"][0]["summary"]


def test_answer_with_stale_question_is_409(client):
    rec = seed_session(answered=2)
    res = client.post(f"/api/quiz/{rec.id}/answer", json={"question_id": "stale", "choice": "A"})
    assert res.status_code == 409


def test_answer_rejects_bad_choice(client):
    rec = seed_session(answered=2)
    res = client.post(f"/api/quiz/{rec.id}/answer", json={"question_id": rec.current.question.id, "choice": "C"})
    assert res.status_code == 422


def test_quiz_start_after_done_is_409(client):
    rec = seed_session(done=True)
    assert client.post(f"/api/quiz/{rec.id}/start").status_code == 409


def test_profile_requires_finished_quiz(client):
    rec = seed_session(answered=5)
    assert client.post(f"/api/profile/{rec.id}").status_code == 409


def test_shop_and_design_require_profile(client):
    rec = seed_session(done=True)
    assert client.post(f"/api/shop/{rec.id}/suggest").status_code == 409
    assert client.post(f"/api/design/{rec.id}/generate", json={"count": 3}).status_code == 409


def test_design_unknown_base_is_404_and_count_validated(client):
    rec = seed_session(done=True, profile=True)
    res = client.post(f"/api/design/{rec.id}/generate", json={"feedback": "bolder", "base_design_id": "x", "count": 1})
    assert res.status_code == 404
    assert client.post(f"/api/design/{rec.id}/generate", json={"count": 4}).status_code == 422


def test_order_requires_known_design(client):
    rec = seed_session(done=True, profile=True, designs=1)
    res = client.post("/api/orders", json={"session_id": rec.id, "design_id": "missing", "spec": {}})
    assert res.status_code == 404
    res = client.post("/api/orders", json={"session_id": "nope", "design_id": rec.designs[0].id, "spec": {}})
    assert res.status_code == 404


def test_context_empty_post_returns_items(client):
    rec = seed_session(context=1)
    res = client.post(f"/api/context/{rec.id}", data={"text": "   "})
    assert res.status_code == 200
    assert [i["id"] for i in res.json()["items"]] == [rec.context[0].item.id]


def test_context_limits(client):
    rec = seed_session()
    files = [("files", (f"f{i}.txt", b"hello", "text/plain")) for i in range(6)]
    assert client.post(f"/api/context/{rec.id}", files=files).status_code == 422
    urls = json.dumps([f"https://example.com/{i}" for i in range(6)])
    assert client.post(f"/api/context/{rec.id}", data={"urls": urls}).status_code == 422
    assert client.post(f"/api/context/{rec.id}", data={"urls": "[not json"}).status_code == 422
    assert client.delete(f"/api/context/{rec.id}/missing").status_code == 404


def test_session_edit_is_persisted():
    import asyncio

    rec = seed_session()

    async def mutate():
        async with store.edit_session(rec.id) as live:
            live.hypothesis = "likes neon"

    asyncio.run(mutate())
    assert store.require_session(rec.id).hypothesis == "likes neon"
