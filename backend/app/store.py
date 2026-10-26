"""SQLite persistence for sessions and orders (JSON blobs), plus the internal session record.

Tables (SPEC section 5):
    sessions(id TEXT PRIMARY KEY, data TEXT /* SessionRecord JSON */, updated_at TEXT)
    orders(id TEXT PRIMARY KEY, data TEXT /* {"order": Order, "session_id": str|None, "requester_hash": str|None} */, created_at TEXT)

Internal session record
-----------------------
``SessionRecord`` is what is stored. It is a superset of the public ``SessionState`` and carries
hidden fields the user never sees. Feature modules read/write it like this::

    from app import store

    async with store.edit_session(session_id) as rec:   # per-session lock; 404 if unknown; saves on exit
        rec.hypothesis = "..."
        rec.answers.append(store.StoredAnswer(...))

    rec = store.require_session(session_id)               # read-only snapshot (raises NotFoundError)

Fields (all JSON-serialisable pydantic models):
    id            str                       session id (secrets.token_urlsafe(9))
    created_at    str                       ISO 8601 UTC
    updated_at    str                       ISO 8601 UTC, bumped by save_session()
    country       Country                   chosen on the landing page
    hypothesis    str                       HIDDEN running personality hypothesis (<= 60 words), quiz.py
    answers       list[StoredAnswer]        each = public Question + HIDDEN dimension + choice + answered_at
    current       StoredQuestion | None     question being shown now (+ HIDDEN dimension), None when done
    quiz_done     bool                      True after round 10 is answered
    context       list[StoredContext]       each = public ContextItem + HIDDEN extracted text (<= 20k chars,
                                            untrusted) + source (filename or URL)
    profile       Profile | None            latest synthesized profile (profile.py)
    designs       list[Design]              every design ever generated, oldest first (design.py)
    products      list[Product]             HIDDEN cache of the latest shop suggestions (shop.py)
    extra         dict[str, Any]            HIDDEN free-form scratch space for feature modules; namespace
                                            your keys, e.g. extra["quiz.seen_refs"]

Speculative-prefetch asyncio tasks are NOT stored here; quiz.py keeps them in memory.

Concurrency: async code must mutate sessions only inside ``edit_session`` (asyncio lock per session,
so a quiz answer and a context upload can't clobber each other). The low-level SQLite calls are
serialized with a process-wide threading lock, so sync code (threadpool routes) is safe too.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .config import get_settings
from .errors import NotFoundError
from .models import (
    AnsweredQuestion,
    Choice,
    ContextItem,
    Country,
    Design,
    Option,
    Order,
    Product,
    Profile,
    Question,
    SessionState,
    UsageSummary,
)

# --------------------------------------------------------------------------- helpers


def new_id() -> str:
    """Opaque URL-safe id (12 chars), used for sessions, orders, questions, designs, items."""
    return secrets.token_urlsafe(9)


def now_iso() -> str:
    """Current UTC time, ISO 8601 with seconds precision, e.g. '2026-10-13T18:04:05+00:00'."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- internal records


class StoredQuestion(BaseModel):
    """A served question plus hidden metadata."""

    question: Question
    dimension: str = Field(description="HIDDEN: trait/taste dimension this round probes, e.g. 'social energy'")
    rationale: str | None = Field(default=None, description="HIDDEN: why the engine picked it (debug only)")
    image_queries: list[str] = Field(default_factory=list, description="HIDDEN: photo queries for A and B")


class StoredAnswer(BaseModel):
    question: Question
    dimension: str
    choice: Choice
    answered_at: str = Field(default_factory=now_iso)
    image_queries: list[str] = Field(default_factory=list, description="HIDDEN: photo queries for A and B")

    @property
    def chosen(self) -> Option:
        """The picked Option (convenience for prompts)."""
        return self.question.option_a if self.choice == "A" else self.question.option_b

    @property
    def rejected(self) -> Option:
        return self.question.option_b if self.choice == "A" else self.question.option_a


class StoredContext(BaseModel):
    item: ContextItem
    text: str = Field(default="", description="HIDDEN: extracted text, <= 20k chars, UNTRUSTED user data")
    source: str | None = Field(default=None, description="HIDDEN: original filename or URL")


class SessionRecord(BaseModel):
    id: str
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    country: Country
    hypothesis: str = ""
    answers: list[StoredAnswer] = Field(default_factory=list)
    current: StoredQuestion | None = None
    quiz_done: bool = False
    context: list[StoredContext] = Field(default_factory=list)
    profile: Profile | None = None
    designs: list[Design] = Field(default_factory=list)
    products: list[Product] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_state(self) -> SessionState:
        """Public projection (drops every HIDDEN field)."""
        return SessionState(
            id=self.id,
            country=self.country,
            answers=[AnsweredQuestion(question=a.question, choice=a.choice) for a in self.answers],
            current_question=self.current.question if self.current else None,
            quiz_done=self.quiz_done,
            context=[c.item for c in self.context],
            profile=self.profile,
            designs=list(self.designs),
        )

    def find_design(self, design_id: str) -> Design | None:
        return next((d for d in self.designs if d.id == design_id), None)


# --------------------------------------------------------------------------- database

_db_lock = threading.RLock()
_db_path: Path | None = None
_initialized_for: Path | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_usage (
    session_id TEXT PRIMARY KEY,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    request_count INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0
);
"""


def configure(db_path: str | Path) -> None:
    """Point the store at a different SQLite file (tests use a temp file) and create tables."""
    global _db_path, _initialized_for
    with _db_lock:
        _db_path = Path(db_path)
        _initialized_for = None
        _session_locks.clear()
    init_db()


def db_path() -> Path:
    return _db_path or get_settings().db_path


def init_db() -> None:
    """Create the data directory and tables if needed. Idempotent; called at app startup."""
    global _initialized_for
    path = db_path()
    with _db_lock:
        if _initialized_for == path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialized_for = path


def _write(sql: str, params: tuple) -> None:
    """Run one write statement on a short-lived connection under the global lock."""
    init_db()
    with _db_lock:
        conn = sqlite3.connect(db_path(), timeout=10)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


def _read_one(sql: str, params: tuple) -> tuple | None:
    init_db()
    with _db_lock:
        conn = sqlite3.connect(db_path(), timeout=10)
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()


# --------------------------------------------------------------------------- sessions


def create_session(country: Country) -> SessionRecord:
    rec = SessionRecord(id=new_id(), country=country)
    save_session(rec)
    return rec


def get_session(session_id: str) -> SessionRecord | None:
    row = _read_one("SELECT data FROM sessions WHERE id = ?", (session_id,))
    return SessionRecord.model_validate_json(row[0]) if row else None


def require_session(session_id: str) -> SessionRecord:
    rec = get_session(session_id)
    if rec is None:
        raise NotFoundError("Session not found. Start again from the home page.")
    return rec


def save_session(rec: SessionRecord) -> None:
    """Upsert the record and bump ``updated_at``. Prefer ``edit_session`` from async code."""
    rec.updated_at = now_iso()
    _write(
        "INSERT INTO sessions (id, data, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
        (rec.id, rec.model_dump_json(), rec.updated_at),
    )


def get_usage(session_id: str) -> UsageSummary:
    row = _read_one(
        "SELECT input_tokens, cached_input_tokens, output_tokens, request_count, estimated_cost_usd "
        "FROM llm_usage WHERE session_id = ?",
        (session_id,),
    )
    return UsageSummary.model_validate(dict(zip(
        ("input_tokens", "cached_input_tokens", "output_tokens", "request_count", "estimated_cost_usd"),
        row,
    ))) if row else UsageSummary()


def add_usage(
    session_id: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float,
) -> None:
    # Accumulate token counts only; prompts and model responses are never persisted.
    _write(
        "INSERT INTO llm_usage "
        "(session_id, input_tokens, cached_input_tokens, output_tokens, request_count, estimated_cost_usd) "
        "VALUES (?, ?, ?, ?, 1, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "input_tokens = input_tokens + excluded.input_tokens, "
        "cached_input_tokens = cached_input_tokens + excluded.cached_input_tokens, "
        "output_tokens = output_tokens + excluded.output_tokens, "
        "request_count = request_count + 1, "
        "estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd",
        (session_id, input_tokens, cached_input_tokens, output_tokens, estimated_cost_usd),
    )


def delete_session(session_id: str) -> bool:
    """Forget a session and everything in it (answers, uploads' text, profile, designs).

    Orders are NOT deleted: each is a self-contained snapshot, and their links may already be
    shared. Returns False if the session didn't exist.
    """
    init_db()
    with _db_lock:
        conn = sqlite3.connect(db_path(), timeout=10)
        try:
            deleted = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount
            conn.execute("DELETE FROM llm_usage WHERE session_id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()
        _session_locks.pop(session_id, None)
    return deleted > 0


def purge_sessions(older_than_days: float) -> int:
    """Delete sessions not touched for ``older_than_days`` (retention); returns how many. Orders stay."""
    if older_than_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
    init_db()
    with _db_lock:
        conn = sqlite3.connect(db_path(), timeout=10)
        try:
            deleted = conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,)).rowcount
            conn.execute("DELETE FROM llm_usage WHERE session_id NOT IN (SELECT id FROM sessions)")
            conn.commit()
        finally:
            conn.close()
    return deleted


# One asyncio.Lock per (event loop, session); recreated if the loop changes (e.g. between tests).
_session_locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def session_lock(session_id: str) -> asyncio.Lock:
    """The asyncio lock guarding mutations of one session. Must be called inside a running loop."""
    loop = asyncio.get_running_loop()
    entry = _session_locks.get(session_id)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _session_locks[session_id] = entry
    return entry[1]


@asynccontextmanager
async def edit_session(session_id: str) -> AsyncIterator[SessionRecord]:
    """Lock, load (NotFoundError if unknown), yield for mutation, then save — unless the body raised.

    Keep slow work (LLM calls, HTTP) OUTSIDE this block where possible: do the slow part first,
    then open ``edit_session`` just to apply the result, re-checking state inside.
    """
    async with session_lock(session_id):
        rec = require_session(session_id)
        yield rec
        save_session(rec)


# --------------------------------------------------------------------------- orders


def save_order(order: Order, session_id: str | None = None, requester_hash: str | None = None) -> None:
    """Insert or update an order. Hidden alongside the public Order: ``session_id`` (who created
    it) and ``requester_hash`` (SHA-256 of the requester's token, orders.py). ``None`` keeps the
    stored value."""
    existing = _read_one("SELECT data FROM orders WHERE id = ?", (order.id,))
    hidden = json.loads(existing[0]) if existing else {}
    blob = json.dumps({
        "order": order.model_dump(mode="json"),
        "session_id": session_id if session_id is not None else hidden.get("session_id"),
        "requester_hash": requester_hash if requester_hash is not None else hidden.get("requester_hash"),
    })
    _write(
        "INSERT INTO orders (id, data, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
        (order.id, blob, order.created_at),
    )


def get_order(order_id: str) -> Order | None:
    row = _read_one("SELECT data FROM orders WHERE id = ?", (order_id,))
    return Order.model_validate(json.loads(row[0])["order"]) if row else None


def get_order_session_id(order_id: str) -> str | None:
    row = _read_one("SELECT data FROM orders WHERE id = ?", (order_id,))
    return json.loads(row[0]).get("session_id") if row else None


def get_order_requester_hash(order_id: str) -> str | None:
    row = _read_one("SELECT data FROM orders WHERE id = ?", (order_id,))
    return json.loads(row[0]).get("requester_hash") if row else None


def count_orders(session_id: str) -> int:
    """How many orders were created from ``session_id``."""
    row = _read_one("SELECT COUNT(*) FROM orders WHERE json_extract(data, '$.session_id') = ?", (session_id,))
    return int(row[0]) if row else 0
