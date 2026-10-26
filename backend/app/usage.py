"""Session attribution and token-only usage accounting for model API responses."""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

ACTIVE_SESSION_ID: ContextVar[str | None] = ContextVar("teedentity_usage_session", default=None)

INPUT_RATE = 0.10 / 1_000_000
CACHED_INPUT_RATE = 0.01 / 1_000_000
OUTPUT_RATE = 0.50 / 1_000_000


def _value(obj: Any, name: str, default: Any = 0) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def record_response_usage(usage: Any) -> None:
    """Persist token totals for the active session, never request or response text."""
    session_id = ACTIVE_SESSION_ID.get()
    if not session_id or usage is None:
        return
    input_tokens = max(0, int(_value(usage, "prompt_tokens", _value(usage, "input_tokens", 0)) or 0))
    output_tokens = max(0, int(_value(usage, "completion_tokens", _value(usage, "output_tokens", 0)) or 0))
    details = _value(usage, "prompt_tokens_details", _value(usage, "input_tokens_details", None))
    cached = max(0, int(_value(details, "cached_tokens", 0) or 0))
    cached = min(cached, input_tokens)
    cost = (input_tokens - cached) * INPUT_RATE + cached * CACHED_INPUT_RATE + output_tokens * OUTPUT_RATE
    from .store import add_usage

    add_usage(session_id, input_tokens, cached, output_tokens, cost)


def session_from_path(path: str) -> str | None:
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) >= 3 and parts[0] == "api" and parts[1] in {"quiz", "context", "profile", "shop", "design"}:
        candidate = parts[2]
        return None if candidate == "warm" else candidate
    return None
