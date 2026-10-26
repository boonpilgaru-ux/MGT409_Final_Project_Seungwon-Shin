"""design.generate latency: hedged concept calls, a grace period after the first concept, and
joined identical in-flight requests (reload during the first concepts)."""

from __future__ import annotations

import asyncio
import time

from app import design, llm, store
from tests.factories import seed_session
from tests.test_design import GOOD, _direction, _reply


def test_concept_calls_are_hedged(fake_llm):
    rec = seed_session(done=True, profile=True)
    fake_llm.on("chat_text", lambda call: _reply("T", GOOD[_direction(call.user)]))
    asyncio.run(design.generate(rec.id, None, None, 3))
    assert {c.kwargs["hedge_after"] for c in fake_llm.calls_of("chat_text")} == {design.DESIGN_HEDGE_AFTER_S}


def _chat_text(cancelled: list[str], stalled: tuple[str, ...] = (), delays: dict[str, float] | None = None):
    """Fake llm.chat_text: concepts in ``stalled`` hang (and record their cancellation)."""

    async def chat_text(model, system, user, max_tokens=1024, **kwargs):
        key = _direction(user)
        if key in stalled:
            try:
                await asyncio.sleep(60)  # the gateway hangs on this one
            except asyncio.CancelledError:
                cancelled.append(key)
                raise
        await asyncio.sleep((delays or {}).get(key, 0.05))
        return _reply(f"Concept {key}", GOOD[key])

    return chat_text


def _timed_generate(session_id: str) -> tuple[list, float]:
    async def run():
        started = time.perf_counter()
        designs = await design.generate(session_id, None, None, 3)
        return designs, time.perf_counter() - started

    return asyncio.run(run())


def test_spare_stands_in_for_a_stalled_concept_after_a_short_wait(monkeypatch):
    rec = seed_session(done=True, profile=True)
    monkeypatch.setattr(design, "DESIGN_GRACE_S", 5.0)
    monkeypatch.setattr(design, "DESIGN_SPARE_WAIT_S", 0.3)
    cancelled: list[str] = []
    monkeypatch.setattr(llm, "chat_text", _chat_text(cancelled, stalled=("ABSTRACT",)))

    designs, elapsed = _timed_generate(rec.id)
    assert 0.3 <= elapsed < 1.5  # 3 concepts were ready: the spare wait, not the 5 s grace period
    assert [d.title for d in designs] == ["Concept TYPE-LED", "Concept ILLUSTRATIVE EMBLEM", "Concept LINE ART"]
    assert cancelled == ["ABSTRACT"]
    assert len(store.require_session(rec.id).designs) == 3


def test_spare_stands_in_for_a_broken_concept_without_waiting_for_its_retry(monkeypatch):
    rec = seed_session(done=True, profile=True)
    monkeypatch.setattr(design, "DESIGN_SPARE_WAIT_S", 5.0)
    calls: list[str] = []

    async def chat_text(model, system, user, max_tokens=1024, **kwargs):
        calls.append(user)
        key = _direction(user)
        await asyncio.sleep(0.2 if key == "ABSTRACT" else 0.05)
        return "Sorry, the SVG got cut: <svg><rect" if key == "ABSTRACT" else _reply(f"Concept {key}", GOOD[key])

    monkeypatch.setattr(llm, "chat_text", chat_text)
    designs, elapsed = _timed_generate(rec.id)
    assert elapsed < 1  # not the 5 s spare wait, nor a retry
    assert [d.title for d in designs] == ["Concept TYPE-LED", "Concept ILLUSTRATIVE EMBLEM", "Concept LINE ART"]
    assert len(calls) == 4 and not any("previous attempt failed" in c for c in calls)


def test_a_direction_a_little_slower_than_the_spare_is_kept(monkeypatch):
    rec = seed_session(done=True, profile=True)
    monkeypatch.setattr(design, "DESIGN_SPARE_WAIT_S", 2.0)
    cancelled: list[str] = []
    monkeypatch.setattr(llm, "chat_text", _chat_text(cancelled, delays={"ILLUSTRATIVE EMBLEM": 0.4}))

    designs, elapsed = _timed_generate(rec.id)
    assert elapsed < 1.5  # returns as soon as the slower direction is in, not after the spare wait
    assert [d.title for d in designs] == ["Concept TYPE-LED", "Concept ILLUSTRATIVE EMBLEM", "Concept ABSTRACT"]


def test_each_ready_concept_restarts_the_grace_period(monkeypatch):
    # a slow gateway: concepts keep arriving, but further apart than one grace period from the first
    rec = seed_session(done=True, profile=True)
    monkeypatch.setattr(design, "DESIGN_GRACE_S", 0.4)
    cancelled: list[str] = []
    delays = {"TYPE-LED": 0.05, "ILLUSTRATIVE EMBLEM": 0.35, "ABSTRACT": 0.65}
    monkeypatch.setattr(llm, "chat_text", _chat_text(cancelled, stalled=("LINE ART",), delays=delays))

    designs, elapsed = _timed_generate(rec.id)
    assert [d.title for d in designs] == ["Concept TYPE-LED", "Concept ILLUSTRATIVE EMBLEM", "Concept ABSTRACT"]
    assert elapsed < 2 and cancelled == ["LINE ART"]


def test_stalled_concepts_no_longer_hold_the_batch(monkeypatch):
    rec = seed_session(done=True, profile=True)
    monkeypatch.setattr(design, "DESIGN_GRACE_S", 0.2)
    cancelled: list[str] = []
    monkeypatch.setattr(llm, "chat_text", _chat_text(cancelled, stalled=("ABSTRACT", "LINE ART")))

    designs, elapsed = _timed_generate(rec.id)
    assert elapsed < 2 and len(designs) == 2  # was: 170 s for the same two designs
    assert [d.title for d in designs] == ["Concept TYPE-LED", "Concept ILLUSTRATIVE EMBLEM"]
    assert sorted(cancelled) == ["ABSTRACT", "LINE ART"]
    assert len(store.require_session(rec.id).designs) == 2


def test_identical_requests_in_flight_share_one_batch(monkeypatch):
    rec = seed_session(done=True, profile=True)
    calls: list[str] = []

    async def chat_text(model, system, user, max_tokens=1024, **kwargs):
        calls.append(user)
        await asyncio.sleep(0.1)
        return _reply(f"Concept {_direction(user)}", GOOD[_direction(user)])

    monkeypatch.setattr(llm, "chat_text", chat_text)

    async def reload_mid_generation():
        first = asyncio.ensure_future(design.generate(rec.id, None, None, 3))
        await asyncio.sleep(0.02)  # the page reloads and asks again
        second = await design.generate(rec.id, None, None, 3)
        return await first, second

    first, second = asyncio.run(reload_mid_generation())
    assert [d.id for d in first] == [d.id for d in second]
    assert len(calls) == 4 and len(store.require_session(rec.id).designs) == 3  # not 8 calls, 6 designs
    assert not design._inflight


def test_different_requests_are_not_joined(monkeypatch):
    rec = seed_session(done=True, profile=True)
    calls: list[str] = []

    async def chat_text(model, system, user, max_tokens=1024, **kwargs):
        calls.append(user)
        await asyncio.sleep(0.05)
        return _reply(f"Concept {_direction(user)}", GOOD[_direction(user)])

    monkeypatch.setattr(llm, "chat_text", chat_text)

    async def both():
        return await asyncio.gather(
            design.generate(rec.id, None, None, 3), design.generate(rec.id, "bolder", None, 3)
        )

    plain, bolder = asyncio.run(both())
    assert len(calls) == 8 and {d.id for d in plain}.isdisjoint({d.id for d in bolder})
