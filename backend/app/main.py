"""FastAPI application: CORS, /api router, health check, and uniform ``{"detail": str}`` errors.

Run from ``backend/``:  uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import openai
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, context, images, quiz, store
from .config import get_settings
from .errors import AppError
from .models import HealthResponse, UsageSummary
from .usage import ACTIVE_SESSION_ID, session_from_path
from .routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("teedentity")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    store.init_db()
    settings = get_settings()
    purged = store.purge_sessions(settings.session_retention_days)
    if purged:
        log.info("Deleted %d session(s) idle for over %g days", purged, settings.session_retention_days)
    # Load the CA bundle once, off the event loop (context links and shop link checks share it).
    await asyncio.to_thread(context._ssl_context)
    log.info("Tee:dentity API ready (portkey key set: %s, db: %s)", settings.portkey_key_set, store.db_path())
    yield
    # Cancel speculative quiz prefetches / background tasks and close pooled HTTP clients.
    await quiz.shutdown()
    await images.aclose()


app = FastAPI(title="Tee:dentity API", version=__version__, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().cors_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Order-Token", "Retry-After"],
)
# Outermost: refuse requests whose Host isn't ours, so a web page that DNS-rebinds its own domain
# to 127.0.0.1 can't drive this API (and spend the Portkey budget) from the visitor's browser.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(get_settings().allowed_hosts))


@app.middleware("http")
async def attribute_llm_usage(request: Request, call_next):
    # Bind session-scoped routes so model calls in their async tasks are attributed.
    session_id = session_from_path(request.url.path)
    token = ACTIVE_SESSION_ID.set(session_id)
    try:
        return await call_next(request)
    finally:
        ACTIVE_SESSION_ID.reset(token)


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(ok=True, portkey_key_set=settings.portkey_key_set, models=settings.model_map())


@app.get("/api/usage/{session_id}", response_model=UsageSummary)
def usage_summary(session_id: str) -> UsageSummary:
    # Return token totals only; prompt and response text are never stored.
    store.require_session(session_id)
    return store.get_usage(session_id)


app.include_router(router, prefix="/api")


# --------------------------------------------------------------------------- error mapping


def _detail(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


@app.exception_handler(AppError)
async def _app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers or None)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return JSONResponse(status_code=exc.status_code, content={"detail": detail}, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Flatten pydantic's error list into one readable string (contract: detail is a string)."""
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path"))
        msg = str(err.get("msg", "invalid")).removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return _detail(422, "; ".join(parts) or "Invalid request.")


@app.exception_handler(openai.APIError)
async def _llm_sdk_error(_: Request, exc: openai.APIError) -> JSONResponse:
    log.warning("Unwrapped LLM error: %s", type(exc).__name__)
    return _detail(502, "Our AI had a hiccup. Please try again.")


@app.exception_handler(NotImplementedError)
async def _not_implemented(_: Request, exc: NotImplementedError) -> JSONResponse:
    return _detail(501, f"Not implemented yet: {exc}")


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return _detail(500, "Something went wrong on our side. Please try again.")
