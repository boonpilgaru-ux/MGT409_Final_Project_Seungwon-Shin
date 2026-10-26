"""Offline stand-in for ``app.llm`` used by the ``fake_llm`` fixture (see conftest.py).

Configure responses per call kind; each call consumes from the matching queue first, then falls
back to a handler, else fails loudly with instructions:

    def test_x(fake_llm):
        fake_llm.json({"archetype": "..."}, schema=Profile)    # next chat_json for Profile
        fake_llm.json(lambda call: {...})                     # next chat_json, any schema
        fake_llm.text("A short summary.")                       # next chat_text
        fake_llm.search('[{"title": ...}]')                   # next web_search_text / web_search_json
        fake_llm.on("chat_json", lambda call: {...})            # handler for every remaining call
        fake_llm.json(LLMError())                              # make the next call fail
        ...
        assert fake_llm.calls[0].model == "gpt-6-luna"

A response may be a dict/list/pydantic model/str (str is parsed like a real model reply), a
callable ``(LLMCall) -> response``, or an Exception instance (raised). With concurrent calls
(asyncio.gather) queue order is not guaranteed — prefer ``on(...)`` handlers that inspect the call.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app import llm as real_llm

KINDS = ("chat_text", "chat_json", "web_search")


@dataclass
class LLMCall:
    kind: str  # "chat_text" | "chat_json" | "web_search_text" | "web_search_json"
    model: str
    system: str | None
    user: str  # user message, or the web-search prompt
    schema: Any = None
    kwargs: dict[str, Any] = field(default_factory=dict)


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[LLMCall] = []
        self._queues: dict[tuple[str, Any], deque] = defaultdict(deque)
        self._handlers: dict[str, Callable[[LLMCall], Any]] = {}

    # ----------------------------------------------------------------- configuration
    def json(self, response: Any, schema: Any = None) -> FakeLLM:
        """Queue a chat_json response (optionally only for calls with this ``schema``)."""
        self._queues[("chat_json", schema)].append(response)
        return self

    def text(self, response: Any) -> FakeLLM:
        self._queues[("chat_text", None)].append(response)
        return self

    def search(self, response: Any) -> FakeLLM:
        """Queue a web search answer (used by web_search_text and web_search_json)."""
        self._queues[("web_search", None)].append(response)
        return self

    def on(self, kind: str, handler: Callable[[LLMCall], Any]) -> FakeLLM:
        """Handle every call of ``kind`` ("chat_text" | "chat_json" | "web_search") not served by a queue."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        self._handlers[kind] = handler
        return self

    def calls_of(self, kind: str) -> list[LLMCall]:
        return [c for c in self.calls if c.kind == kind]

    # ----------------------------------------------------------------- internals
    def _next(self, kind: str, call: LLMCall) -> Any:
        for key in ((kind, call.schema), (kind, None)):
            queue = self._queues.get(key)
            if queue:
                response = queue.popleft()
                break
        else:
            handler = self._handlers.get(kind)
            if handler is None:
                raise AssertionError(
                    f"Unexpected {call.kind} call (model={call.model!r}, schema={_name(call.schema)}). "
                    f"Configure it with fake_llm.{'json' if kind == 'chat_json' else kind.split('_')[-1]}(...) "
                    f"or fake_llm.on({kind!r}, handler)."
                )
            response = handler
        if callable(response) and not isinstance(response, (type, BaseModel)):
            response = response(call)
        if isinstance(response, BaseException):
            raise response
        return response

    @staticmethod
    def _coerce(schema: Any, response: Any) -> Any:
        if isinstance(response, str):
            response = real_llm.extract_json(response)
        elif isinstance(response, BaseModel):
            response = response.model_dump(mode="json")
        elif isinstance(response, list):
            response = [r.model_dump(mode="json") if isinstance(r, BaseModel) else r for r in response]
        return real_llm._validate(schema, response)

    # ----------------------------------------------------------------- fake API (same signatures)
    async def chat_text(self, model, system, user, max_tokens=1024, **kwargs) -> str:
        call = LLMCall("chat_text", model, system, user, None, {"max_tokens": max_tokens, **kwargs})
        self.calls.append(call)
        response = self._next("chat_text", call)
        return response if isinstance(response, str) else json.dumps(response)

    async def chat_json(self, model, system, user, schema, max_tokens=4096, **kwargs):
        call = LLMCall("chat_json", model, system, user, schema, {"max_tokens": max_tokens, **kwargs})
        self.calls.append(call)
        return self._coerce(schema, self._next("chat_json", call))

    async def web_search_text(self, model, prompt, **kwargs) -> str:
        call = LLMCall("web_search_text", model, kwargs.get("instructions"), prompt, None, kwargs)
        self.calls.append(call)
        response = self._next("web_search", call)
        if isinstance(response, BaseModel):
            return response.model_dump_json()
        return response if isinstance(response, str) else json.dumps(response)

    async def web_search_json(self, model, prompt, schema, **kwargs):
        call = LLMCall("web_search_json", model, kwargs.get("instructions"), prompt, schema, kwargs)
        self.calls.append(call)
        return self._coerce(schema, self._next("web_search", call))


def _name(schema: Any) -> str:
    return getattr(schema, "__name__", None) or repr(schema)
