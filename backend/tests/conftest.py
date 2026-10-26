"""Shared pytest fixtures. Every test runs fully offline against a throwaway SQLite file.

Autouse (always on):
    * temp DB      store points at <tmp_path>/app.db
    * offline      real HTTP via httpx / httpx2 transports raises ConnectError (MockTransport still works)
    * fake LLM     app.llm.chat_text / chat_json / web_search_text / web_search_json are replaced by a
                   FakeLLM with no canned responses, so an unexpected LLM call fails with a clear message

Fixtures to request:
    client          FastAPI TestClient (lifespan run)
    fake_llm        the installed FakeLLM; queue responses / set handlers (see tests/fake_llm.py)
    fake_images     replaces images.find_pair with a deterministic async fake; ``.calls`` lists the photo
                    queries (card A, card B, ...), ``.specs`` / ``.excludes`` / ``.prefers`` / ``.contexts`` /
                    ``.hurries`` the arguments; set ``.result`` (or ``.by_query[query]``) to an OptionImage or
                    None. Like the real one it returns photos for both cards or for neither.
Data builders live in tests/factories.py (e.g. ``seed_session(done=True, profile=True)``).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

# Before the app is imported: TestClient sends "Host: testserver", and TrustedHostMiddleware
# (main.py) only answers allowed hosts.
os.environ.setdefault("TEE_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import images, llm, ratelimit, store  # noqa: E402
from app.main import app  # noqa: E402
from app.models import OptionImage  # noqa: E402
from tests.fake_llm import FakeLLM  # noqa: E402

try:  # the openai SDK (3.x) ships its own fork, httpx2; block it too when present
    import httpx2
except ImportError:  # pragma: no cover
    httpx2 = None

# HTTP client libraries whose real transports are disabled during tests.
_HTTP_LIBS = [lib for lib in (httpx, httpx2) if lib is not None]


@pytest.fixture(autouse=True)
def _temp_db(tmp_path) -> Iterator[None]:
    store.configure(tmp_path / "app.db")
    yield


@pytest.fixture(autouse=True)
def _fresh_rate_limits() -> Iterator[None]:
    """Every test starts with full per-client buckets (they are process-wide)."""
    ratelimit.bucket.reset()
    yield
    ratelimit.bucket.reset()


@pytest.fixture(autouse=True)
def _offline(monkeypatch) -> None:
    """Make any real network I/O fail fast; ``MockTransport`` (a different class) is unaffected."""
    for lib in _HTTP_LIBS:

        def _blocked_sync(self, request, _lib=lib):
            raise _lib.ConnectError("Real network is disabled in tests; use MockTransport.", request=request)

        async def _blocked_async(self, request, _lib=lib):
            raise _lib.ConnectError("Real network is disabled in tests; use MockTransport.", request=request)

        monkeypatch.setattr(lib.HTTPTransport, "handle_request", _blocked_sync)
        monkeypatch.setattr(lib.AsyncHTTPTransport, "handle_async_request", _blocked_async)


@pytest.fixture(autouse=True)
def _install_fake_llm(monkeypatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(llm, "chat_text", fake.chat_text)
    monkeypatch.setattr(llm, "chat_json", fake.chat_json)
    monkeypatch.setattr(llm, "web_search_text", fake.web_search_text)
    monkeypatch.setattr(llm, "web_search_json", fake.web_search_json)
    return fake


@pytest.fixture
def fake_llm(_install_fake_llm: FakeLLM) -> FakeLLM:
    return _install_fake_llm


class FakeImages:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.specs: list[images.PhotoSpec] = []
        self.excludes: list[frozenset[str]] = []
        self.prefers: list[tuple[str, ...]] = []
        self.contexts: list[str] = []
        self.hurries: list[object] = []
        self.result: OptionImage | None = OptionImage(
            url="https://images.example/photo.jpg", credit="Test Creator / CC BY-SA 4.0 via Wikipedia",
            source_url="https://images.example/landing",
        )
        # Optional per-query override: query -> OptionImage | None (falls back to ``result``).
        self.by_query: dict[str, OptionImage | None] = {}

    def _one(self, spec: images.PhotoSpec, exclude: frozenset[str]) -> OptionImage | None:
        self.calls.append(spec.query)
        self.specs.append(spec)
        self.excludes.append(exclude)
        self.prefers.append(tuple(spec.prefer))
        image = self.by_query.get(spec.query, self.result)
        return None if image is not None and image.url in exclude else image

    async def find_pair(self, a, b, *, exclude=(), context="", hurry=None):
        self.contexts.append(context)
        self.hurries.append(hurry)
        skip = frozenset(exclude)
        img_a, img_b = self._one(a, skip), self._one(b, skip)
        if img_a is None or img_b is None:
            return None, None
        if img_a.url == img_b.url:  # the real lookup never shows one photo on both cards
            img_b = img_b.model_copy(update={"url": f"{img_b.url}?b"})
        return img_a, img_b


@pytest.fixture
def fake_images(monkeypatch) -> FakeImages:
    fake = FakeImages()
    monkeypatch.setattr(images, "find_pair", fake.find_pair)
    return fake


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
