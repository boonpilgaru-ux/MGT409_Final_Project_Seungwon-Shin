from types import SimpleNamespace

import pytest

from app import store
from app.config import get_settings
from app.models import Country
from app.usage import ACTIVE_SESSION_ID, record_response_usage
from tests.factories import seed_session


def test_usage_counts_both_api_shapes_and_course_rates(client):
    rec = seed_session()
    token = ACTIVE_SESSION_ID.set(rec.id)
    try:
        record_response_usage(SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            prompt_tokens_details=SimpleNamespace(cached_tokens=25),
        ))
        record_response_usage(SimpleNamespace(
            input_tokens=50,
            output_tokens=15,
            input_tokens_details=SimpleNamespace(cached_tokens=10),
        ))
    finally:
        ACTIVE_SESSION_ID.reset(token)

    response = client.get(f"/api/usage/{rec.id}")
    assert response.status_code == 200
    assert response.json() == {
        "input_tokens": 150,
        "cached_input_tokens": 35,
        "output_tokens": 35,
        "request_count": 2,
        "estimated_cost_usd": pytest.approx(0.00002935),
    }


def test_usage_endpoint_requires_an_existing_session(client):
    assert client.get("/api/usage/missing-session").status_code == 404


def test_all_model_roles_stay_on_course_luna(monkeypatch):
    monkeypatch.setenv("TEE_QUESTION_MODEL", "another-model")
    monkeypatch.setenv("TEE_PROFILE_MODEL", "another-model")
    get_settings.cache_clear()
    try:
        assert set(get_settings().model_map().values()) == {"gpt-6-luna"}
    finally:
        get_settings.cache_clear()
