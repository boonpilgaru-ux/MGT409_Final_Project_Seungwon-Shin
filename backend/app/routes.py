"""All /api routes (SPEC section 4). Thin: validate state, call feature modules, shape responses.

State checks here (404 unknown ids, 409 wrong state, 413/422 upload limits) run BEFORE the feature
module is called, so the contract holds even while a module is still a stub. Modules re-check
under the session lock where races matter. Domain errors map to HTTP in main.py.

Endpoints that spend money (model calls, web searches, stored SVG snapshots) are charged to the
caller's IP with ``limit(cost)`` (ratelimit.py); 429 + Retry-After when the bucket is empty.
"""

from __future__ import annotations

import json
import ntpath
import posixpath
from typing import Annotated

from fastapi import APIRouter, Body, Header, Request, Response
from starlette.datastructures import UploadFile

from . import context, design, orders, profile, quiz, shop, store
from .errors import BadInputError, ConflictError, LengthRequired, NotFoundError, PayloadTooLarge
from .models import (
    CONTEXT_MAX_FILE_BYTES,
    CONTEXT_MAX_FILES,
    CONTEXT_MAX_NOTES,
    CONTEXT_MAX_TEXT_CHARS,
    CONTEXT_MAX_URLS,
    Answer,
    AnswerResult,
    ContextResponse,
    CreateOrderRequest,
    CreateSessionRequest,
    Customer,
    DesignGenerateRequest,
    DesignResponse,
    Order,
    Price,
    ProductSpec,
    Profile,
    Question,
    SessionState,
    ShopResponse,
    UploadedFile,
    WarmRequest,
)
from .ratelimit import charge, client_key, limit

router = APIRouter()

# --------------------------------------------------------------------------- sessions


@router.post("/session", response_model=SessionState, dependencies=[limit(1)])
def create_session(body: CreateSessionRequest) -> SessionState:
    return store.create_session(body.country).to_state()


@router.get("/session/{session_id}", response_model=SessionState)
def get_session(session_id: str) -> SessionState:
    return store.require_session(session_id).to_state()


@router.delete("/session/{session_id}", status_code=204, response_class=Response)
async def delete_session(session_id: str) -> Response:
    """Extra (not in the SPEC table): "Start over" deletes the session and its uploads' text.
    Orders are self-contained snapshots and stay, so shared order links keep working."""
    await quiz.forget_session(session_id)
    if not store.delete_session(session_id):
        raise NotFoundError("Session not found.")
    return Response(status_code=204)


# --------------------------------------------------------------------------- balance game


@router.post("/quiz/warm", status_code=202, response_class=Response, dependencies=[limit(1)])
async def warm_quiz(body: WarmRequest) -> Response:
    """Extra (not in the SPEC table): the landing page pre-generates round 1 for the chosen country
    so the first question is ready when the game starts. Fire-and-forget; always 202."""
    quiz.warm(body.country)
    return Response(status_code=202)


@router.post("/quiz/{session_id}/start", response_model=Question, dependencies=[limit(1)])
async def start_quiz(session_id: str) -> Question:
    rec = store.require_session(session_id)
    if rec.quiz_done:
        raise ConflictError("You've already finished the game.")
    return await quiz.start_quiz(session_id)


@router.post("/quiz/{session_id}/answer", response_model=AnswerResult, dependencies=[limit(1)])
async def answer_quiz(session_id: str, body: Answer) -> AnswerResult:
    rec = store.require_session(session_id)
    if rec.quiz_done:
        raise ConflictError("You've already finished the game.")
    if rec.current is None:
        raise ConflictError("The game hasn't started yet.")
    if rec.current.question.id != body.question_id:
        raise ConflictError("That question is no longer current. Reload to continue.")
    return await quiz.answer(session_id, body)


# --------------------------------------------------------------------------- about you (context)


def _safe_filename(name: str | None) -> str:
    base = ntpath.basename(posixpath.basename(name or "")).strip()
    return (base or "upload")[:200]


async def _read_upload(upload: UploadFile) -> UploadedFile:
    """Read an upload into memory, refusing anything over the per-file limit."""
    name = _safe_filename(upload.filename)
    chunks: list[bytes] = []
    size = 0
    while chunk := await upload.read(1024 * 1024):
        size += len(chunk)
        if size > CONTEXT_MAX_FILE_BYTES:
            raise PayloadTooLarge(f'"{name}" is over {CONTEXT_MAX_FILE_BYTES // (1024 * 1024)} MB.')
        chunks.append(chunk)
    return UploadedFile(name, b"".join(chunks), upload.content_type or "application/octet-stream")


_TOO_MANY_LINKS = f"You can add up to {CONTEXT_MAX_URLS} links in total."
# Whole multipart body: every allowed file at full size, plus notes, the urls JSON and form overhead.
CONTEXT_MAX_BODY_BYTES = CONTEXT_MAX_FILES * CONTEXT_MAX_FILE_BYTES + 2 * 1024 * 1024


def _parse_urls(values: list[str]) -> list[str]:
    """``urls`` is a JSON string array; repeated plain ``urls`` fields are accepted too.

    The raw count is bounded while collecting (twice the session cap; the frontend never sends
    duplicates), so a form stuffed with a million links fails fast instead of de-duplicating them.
    """
    urls: list[str] = []
    for raw in values:
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, RecursionError) as err:  # "[[[[..." nests past the recursion limit
                raise BadInputError("`urls` must be a JSON array of strings.") from err
            if not isinstance(parsed, list) or not all(isinstance(u, str) for u in parsed):
                raise BadInputError("`urls` must be a JSON array of strings.")
            urls.extend(parsed)
        else:
            urls.append(raw)
        if len(urls) > CONTEXT_MAX_URLS * 2:
            raise BadInputError(_TOO_MANY_LINKS)
    cleaned: list[str] = []
    seen: set[str] = set()
    for url in (u.strip() for u in urls):
        if not url or url in seen:
            continue
        if len(url) > 2048:
            raise BadInputError("One of the links is too long.")
        seen.add(url)
        cleaned.append(url)
    return cleaned


def _check_body_size(request: Request) -> None:
    """Refuse oversized uploads BEFORE Starlette spools them to disk (it doesn't cap file parts).

    Browsers always send Content-Length (and never Transfer-Encoding) for a FormData fetch. A
    chunked body is refused rather than counted, even when it also carries a Content-Length: some
    servers (uvicorn's h11) frame such a body by Transfer-Encoding and ignore the length, so the
    declared size would prove nothing. (Deployments should also cap bodies at the proxy.)
    """
    length = request.headers.get("content-length")
    if length is None or "transfer-encoding" in request.headers:
        raise LengthRequired("Send the upload with a Content-Length header.")
    try:
        size = int(length)
    except ValueError:
        raise BadInputError("Invalid Content-Length header.") from None
    if size > CONTEXT_MAX_BODY_BYTES:
        raise PayloadTooLarge(_BODY_TOO_LARGE)


def _capped(request: Request, max_bytes: int) -> Request:
    """``request`` whose body stops being read past ``max_bytes`` (413), whatever the headers said.

    Defense in depth behind ``_check_body_size``: counts the bytes actually received, so a body
    that is longer than its Content-Length claims is never spooled to disk in full.
    """
    received = 0

    async def receive():
        nonlocal received
        message = await request.receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > max_bytes:
                raise PayloadTooLarge(_BODY_TOO_LARGE)
        return message

    return Request(request.scope, receive)


_BODY_TOO_LARGE = f"Uploads are limited to {CONTEXT_MAX_FILES} files of {CONTEXT_MAX_FILE_BYTES // (1024 * 1024)} MB each."


@router.post("/context/{session_id}", response_model=ContextResponse, dependencies=[limit(1)])
async def add_context(session_id: str, request: Request) -> ContextResponse:
    """multipart/form-data: ``files`` (or ``files[]``) 0-5, ``urls`` JSON string array, ``text``.

    Costs 1 rate-limit token, plus 1 per PDF / Word file (each is parsed in a child process).
    """
    rec = store.require_session(session_id)
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type and "application/x-www-form-urlencoded" not in content_type:
        raise BadInputError("Send the form as multipart/form-data.")
    _check_body_size(request)

    # Both "files" and "files[]" count toward max_files, and empty file inputs are filtered below.
    form = await _capped(request, CONTEXT_MAX_BODY_BYTES).form(max_files=CONTEXT_MAX_FILES * 2, max_fields=20)
    try:
        uploads = [
            item
            for key in ("files", "files[]")
            for item in form.getlist(key)
            if isinstance(item, UploadFile) and (item.filename or item.size)
        ]
        url_values = [v for key in ("urls", "urls[]") for v in form.getlist(key) if isinstance(v, str)]
        urls = _parse_urls(url_values)
        texts = [v.strip() for v in form.getlist("text") if isinstance(v, str) and v.strip()]
        text = "\n\n".join(texts) or None

        file_count = sum(1 for c in rec.context if c.item.kind == "file")
        url_count = sum(1 for c in rec.context if c.item.kind == "url")
        note_count = sum(1 for c in rec.context if c.item.kind == "text")
        if file_count + len(uploads) > CONTEXT_MAX_FILES:
            raise BadInputError(f"You can add up to {CONTEXT_MAX_FILES} files in total.")
        if url_count + len(urls) > CONTEXT_MAX_URLS:
            raise BadInputError(_TOO_MANY_LINKS)
        if text and len(text) > CONTEXT_MAX_TEXT_CHARS:
            raise BadInputError(f"Notes are limited to {CONTEXT_MAX_TEXT_CHARS:,} characters.")
        if text and note_count >= CONTEXT_MAX_NOTES:
            raise BadInputError(f"You can add up to {CONTEXT_MAX_NOTES} notes. Delete one to add another.")

        files = [await _read_upload(u) for u in uploads]
    finally:
        await form.close()

    if not (files or urls or text):
        return ContextResponse(items=[c.item for c in rec.context])
    if documents := sum(1 for f in files if context.parser_kind(f)):
        charge(request, documents)
    items = await context.add_context(session_id, files, urls, text, client=client_key(request))
    return ContextResponse(items=items)


@router.delete("/context/{session_id}/{item_id}", response_model=ContextResponse)
async def remove_context(session_id: str, item_id: str) -> ContextResponse:
    rec = store.require_session(session_id)
    if not any(c.item.id == item_id for c in rec.context):
        raise NotFoundError("That item doesn't exist anymore.")
    return ContextResponse(items=await context.remove_context(session_id, item_id))


# --------------------------------------------------------------------------- profile / shop / design


@router.post("/profile/{session_id}", response_model=Profile, dependencies=[limit(2)])
async def build_profile(session_id: str) -> Profile:
    rec = store.require_session(session_id)
    if not rec.quiz_done:
        raise ConflictError("Finish the balance game first.")
    return await profile.build_profile(session_id)


@router.post("/shop/{session_id}/suggest", response_model=ShopResponse, dependencies=[limit(3)])
async def suggest_products(session_id: str) -> ShopResponse:
    rec = store.require_session(session_id)
    if rec.profile is None:
        raise ConflictError("Reveal your profile first.")
    return ShopResponse(products=await shop.suggest(session_id))


@router.post("/design/{session_id}/generate", response_model=DesignResponse, dependencies=[limit(3)])
async def generate_designs(
    session_id: str,
    body: Annotated[DesignGenerateRequest | None, Body()] = None,
) -> DesignResponse:
    body = body or DesignGenerateRequest()
    rec = store.require_session(session_id)
    if rec.profile is None:
        raise ConflictError("Reveal your profile first.")
    if body.base_design_id and rec.find_design(body.base_design_id) is None:
        raise NotFoundError("That design doesn't exist in this session.")
    designs = await design.generate(session_id, body.feedback, body.base_design_id, body.count)
    return DesignResponse(designs=designs)


# --------------------------------------------------------------------------- orders


@router.post("/quote", response_model=Price)
def quote(spec: ProductSpec) -> Price:
    """Extra (not in SPEC table): live price estimate for the Studio before an order exists."""
    return orders.price_for(spec)


@router.post("/orders", response_model=Order, dependencies=[limit(1)])
def create_order(body: CreateOrderRequest) -> Order:
    rec = store.require_session(body.session_id)
    if rec.find_design(body.design_id) is None:
        raise NotFoundError("That design doesn't exist in this session.")
    return orders.create_order(body.session_id, body.design_id, body.spec)


def _require_order(order_id: str) -> Order:
    order = orders.get_order(order_id)
    if order is None:
        raise NotFoundError("Order not found.")
    return order


@router.get("/orders/{order_id}", response_model=Order)
def get_order(order_id: str) -> Order:
    """Public share view: the customer's email is masked and the address hidden (orders.py)."""
    return orders.public_view(_require_order(order_id))


@router.post("/orders/{order_id}/request", response_model=Order)
def request_order(
    order_id: str,
    body: Customer,
    response: Response,
    x_order_token: Annotated[str | None, Header(max_length=200)] = None,
) -> Order:
    """Demo checkout. The first request answers with an ``X-Order-Token`` header; updating an
    already-requested order requires sending it back (403 otherwise). The response echoes the
    full customer, since the caller just typed it."""
    _require_order(order_id)
    order, token = orders.request_order(order_id, body, x_order_token)
    if token:
        response.headers[orders.ORDER_TOKEN_HEADER] = token
    return order


@router.get(
    "/orders/{order_id}/print.svg",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}}},
)
def print_file(order_id: str) -> Response:
    order = _require_order(order_id)
    return Response(
        content=orders.print_svg(order),
        media_type="image/svg+xml",
        headers={
            "Content-Disposition": f'attachment; filename="teedentity-{order.id}.svg"',
            # Belt and braces: even if opened inline, nothing in the SVG may run or load.
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
            "X-Content-Type-Options": "nosniff",
        },
    )
