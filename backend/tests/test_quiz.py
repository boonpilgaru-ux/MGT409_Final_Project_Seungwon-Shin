"""Balance-game engine (app/quiz.py): generation, validation, prefetch, session flows. Fully offline."""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app import images, llm, quiz, store
from app.errors import ConflictError, LLMError
from app.models import QUIZ_TOTAL, Answer, Country, OptionImage
from tests.factories import seed_session

ROUND_RE = re.compile(r"Round (\d+) of \d+")
PICK_RE = re.compile(r"-> picked ([AB])")


def round_of(user: str) -> int:
    return int(ROUND_RE.search(user).group(1))


def last_pick(user: str) -> str | None:
    picks = PICK_RE.findall(user)
    return picks[-1] if picks else None


def payload(n: int, pick: str | None = None, **overrides: Any) -> dict:
    """A valid GenQuestion dict for round ``n`` (unique labels/dimension/reference per round)."""
    data = {
        "hypothesis": f"after {pick or '-'} at round {n}",
        "dimension": f"axis number {n}",
        "why": "test",
        "local_reference": f"Tteokbokki alley {n}",
        "twist": "would_you_rather" if n % 3 == 0 else "none",  # every third round is a twist round
        "prompt": f"Frame number {n} here:",
        "option_a": {"label": f"Tteokbokki take {n}", "sublabel": f"떡볶이 {n}", "emoji": "🌶️",
                     "color": "#ff3b30", "image_query": f"tteokbokki {n}"},
        "option_b": {"label": f"Hotteok take {n}", "sublabel": f"호떡 {n}", "emoji": "🥞",
                     "color": "#2f5bff", "image_query": f"hotteok {n}"},
    }
    for key, value in overrides.items():
        if isinstance(value, dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return data


class ScriptedLLM:
    """Async replacement for ``llm.chat_json``; ``behavior(user, schema, kwargs)`` may be async."""

    def __init__(self, behavior=None) -> None:
        self.calls: list[SimpleNamespace] = []
        self.behavior = behavior or self.default

    @staticmethod
    def default(user: str, schema: Any, kwargs: dict) -> Any:
        if schema is quiz.HypothesisUpdate:
            return {"hypothesis": "final hypothesis"}
        return payload(round_of(user), last_pick(user))

    async def chat_json(self, model, system, user, schema, max_tokens=4096, **kwargs):
        call = SimpleNamespace(model=model, system=system, user=user, schema=schema, kwargs=kwargs)
        self.calls.append(call)
        result = self.behavior(user, schema, kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return schema.model_validate(result)

    def rounds(self) -> Counter:
        return Counter(round_of(c.user) for c in self.calls if c.schema is quiz.GenQuestion)


@pytest.fixture
def scripted(monkeypatch) -> ScriptedLLM:
    fake = ScriptedLLM()
    monkeypatch.setattr(llm, "chat_json", fake.chat_json)
    return fake


@pytest.fixture
def photos(monkeypatch) -> list[str]:
    """images.find_pair returning a distinct photo per query; yields the list of queries."""
    queries: list[str] = []

    async def find_pair(a, b, *, exclude=(), context="", hurry=None):
        queries.extend([a.query, b.query])
        return tuple(
            OptionImage(url=f"https://img.example/{s.query.replace(' ', '-')}.jpg", credit="Ann / CC BY via Wikipedia")
            for s in (a, b)
        )

    monkeypatch.setattr(images, "find_pair", find_pair)
    return queries


@pytest.fixture(autouse=True)
async def _quiz_cleanup(monkeypatch):
    monkeypatch.setattr(quiz, "_effort", {"value": "none"})
    yield
    await quiz.shutdown()


async def settle() -> None:
    """Let every in-flight prefetch/background task finish."""
    for _ in range(5):
        tasks = [e.task for e in quiz._prefetch.values()] + list(quiz._starts.values()) + list(quiz._background)
        pending = [t for t in tasks if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


# --------------------------------------------------------------------------- pure helpers


def test_depth_curve_boundaries():
    assert [quiz.depth_for_round(i) for i in (1, 3, 4, 7, 8, 10)] == [
        "broad", "broad", "focused", "focused", "specific", "specific",
    ]


def test_local_requirement_keeps_two_thirds_local():
    local = 0
    required_rounds = []
    for index in range(1, QUIZ_TOTAL + 1):
        if quiz._local_required(index, local):
            local += 1
            required_rounds.append(index)
    assert local == 7  # ceil(2/3 * 10), even if every optional round goes universal
    assert required_rounds[:2] == [1, 2] and 3 not in required_rounds


def test_dimension_similarity():
    assert quiz._dims_similar("Social energy", "social energy (big crowd vs small circle)")
    assert quiz._dims_similar("nostalgia vs novelty", "Nostalgia versus novelty")
    assert not quiz._dims_similar("color temperature", "typography vibe")
    assert not quiz._dims_similar("axis number 3", "axis number 4")


def test_image_query_strips_own_country():
    def q(text: str, code: str, name: str) -> str:
        option = quiz.GenOption(label="x", emoji="x", color="#000000", image_query=text)
        return quiz._image_query(option, Country(code=code, name=name))

    assert q("Brazilian embroidery hoop", "BR", "Brazil") == "embroidery hoop"
    assert q("Brazil vintage postcard", "BR", "Brazil") == "vintage postcard"
    assert q("Korean home interior", "KR", "South Korea") == "home interior"
    assert q("Seoul subway", "KR", "South Korea") == "Seoul subway"
    assert q("indie bookstore", "IN", "India") == "indie bookstore"
    assert q("Korea", "KR", "South Korea") == "Korea"  # never empties the query


def _snap(answers: int = 0, **kw) -> quiz._Snapshot:
    rec = seed_session(answered=answers, started=False, **kw)
    return quiz._Snapshot.of(rec)


def test_problems_accepts_a_good_draft():
    draft = quiz.GenQuestion.model_validate(payload(1))
    assert quiz._problems(draft, _snap(), must_local=True) == ([], [])


@pytest.mark.parametrize(
    ("overrides", "kind", "needle"),
    [
        ({"option_b": {"label": "Tteokbokki take 1"}}, "hard", "same label"),
        ({"option_a": {"label": "one two three four five six seven eight"}}, "soft", "label has 8 words"),
        ({"option_a": {"label": "one two three four five six seven eight nine ten eleven"}}, "hard", "label has 11"),
        ({"prompt": "one two three four five six seven eight nine ten eleven twelve thirteen"}, "soft", "prompt has 13"),
        ({"prompt": "Who wins the election this year?"}, "hard", "off-limits"),
        ({"option_b": {"color": "#ff3b31"}}, "soft", "colors are nearly identical"),
        ({"option_b": {"image_query": "tteokbokki 1"}}, "soft", "same image_query"),
        ({"option_a": {"image_query": "a b c d e f"}}, "soft", "image_query must be 2-5 words"),
        ({"local_reference": None}, "soft", "REQUIRES a specific local reference"),
        ({"prompt": "Plan it or wing it?"}, "soft", 'asks "X or Y?"'),
    ],
)
def test_problems_flags_violations(overrides, kind, needle):
    draft = quiz.GenQuestion.model_validate(payload(1, **overrides))
    hard, soft = quiz._problems(draft, _snap(), must_local=True)
    found = hard if kind == "hard" else soft
    assert any(needle in p for p in found), (hard, soft)


def test_problems_flags_repeated_dimension_and_reused_options():
    snap = _snap(answers=3)  # factories: dimensions "dimension 1..3", labels Tteokbokki / Hotteok
    draft = quiz.GenQuestion.model_validate(
        payload(4, dimension="Dimension 3", prompt="Pick one, forever!", option_a={"label": "Tteokbokki"})
    )
    _, soft = quiz._problems(draft, snap, must_local=False)
    assert any("already covered" in p for p in soft)
    assert any("repeats one from an earlier round" in p for p in soft)
    assert any("prompt frame repeats round 3" in p for p in soft)


def test_invalid_hex_color_is_a_schema_error():
    with pytest.raises(ValueError):
        quiz.GenQuestion.model_validate(payload(1, option_a={"color": "chili red"}))


def test_tidy_normalizes_output():
    draft = quiz.GenQuestion.model_validate(
        payload(
            1,
            hypothesis=" ".join(["word"] * 80),
            option_a={"label": "  Pojangmacha   night ", "sublabel": "pojangmacha night", "emoji": " "},
            option_b={"image_query": "one two three four five six seven"},
        )
    )
    tidy = quiz._tidy(draft)
    assert tidy.option_a.label == "Pojangmacha night"
    assert tidy.option_a.sublabel is None  # duplicate of the label
    assert tidy.option_a.emoji == "✨"
    assert tidy.option_b.image_query == "one two three four five"
    assert len(tidy.hypothesis.split()) == quiz.HYPOTHESIS_MAX_WORDS


def test_user_prompt_carries_country_history_depth_and_hypothesis():
    rec = seed_session(answered=5, started=False, hypothesis="Likes cozy nights and spicy food.")
    prompt = quiz._build_user_prompt(quiz._Snapshot.of(rec), must_local=True)
    assert "Country: South Korea (KR)" in prompt
    assert "Round 6 of 10. Depth target: focused" in prompt
    assert "Local grounding: REQUIRED" in prompt
    assert "Running hypothesis: Likes cozy nights and spicy food." in prompt
    # full history: prompt, both options, chosen side and dimension for every round
    assert prompt.count("-> picked") == 5
    assert 'R1 [broad | dimension 1] "Pick one. Forever." A: Tteokbokki (떡볶이) / B: Hotteok (호떡) -> picked A' in prompt
    assert "Latest pick:" in prompt and "Dimensions already covered" in prompt
    assert "Write round 6 now." in prompt


# --------------------------------------------------------------------------- generation


async def test_generate_retries_once_on_violation_and_uses_corrected_draft(scripted, fake_images):
    drafts = iter([
        payload(1, option_b={"label": "Tteokbokki take 1"}),  # identical options: hard violation
        payload(1, option_a={"label": "Fixed A"}, option_b={"label": "Fixed B"}),
    ])
    scripted.behavior = lambda user, schema, kw: next(drafts)
    generated = await quiz._generate(_snap())
    assert [c.schema for c in scripted.calls] == [quiz.GenQuestion, quiz.GenQuestion]
    assert "Your previous draft broke these rules" in scripted.calls[1].user
    assert "same label" in scripted.calls[1].user
    q = generated.stored.question
    assert (q.option_a.label, q.option_b.label) == ("Fixed A", "Fixed B")


async def test_generate_raises_when_both_drafts_are_unusable(scripted, fake_images):
    scripted.behavior = lambda user, schema, kw: payload(1, prompt="Pick a president", option_b={"label": "Tteokbokki take 1"})
    with pytest.raises(LLMError):
        await quiz._generate(_snap())
    assert len(scripted.calls) == 2


async def test_generate_accepts_soft_issues_after_retry(scripted, fake_images):
    scripted.behavior = lambda user, schema, kw: payload(1, option_a={"label": "one two three four five six seven eight"})
    generated = await quiz._generate(_snap())
    assert generated.stored.question.option_a.label.startswith("one two")
    assert len(scripted.calls) == 2


async def test_generate_sends_nonce_and_settings(scripted, fake_images):
    await quiz._generate(_snap())
    await quiz._generate(_snap())
    first, second = scripted.calls
    assert first.model == "gpt-6-luna"
    assert first.kwargs["reasoning_effort"] == "none"
    assert first.user != second.user  # nonce defeats gateway response caches
    assert re.search(r"\(request [0-9a-f]{8}\)$", first.user)


async def test_generate_asks_for_both_photos_in_one_call(scripted, monkeypatch):
    seen: list[dict] = []
    photo_a = OptionImage(url="https://img.example/a.jpg", credit="c", source_url=None)
    photo_b = OptionImage(url="https://img.example/b.jpg", credit="c", source_url=None)

    async def find_pair(a, b, *, exclude=(), context="", hurry=None):
        seen.append({"a": a, "b": b, "exclude": exclude, "context": context, "hurry": hurry})
        return photo_a, photo_b

    monkeypatch.setattr(images, "find_pair", find_pair)
    scripted.behavior = lambda user, schema, kw: payload(
        1, option_a={"wiki_lang": "ko", "wiki_title": "떡볶이"}, option_b={"wiki_lang": "ko", "wiki_title": "호떡"}
    )
    wanted = asyncio.Event()
    q = (await quiz._generate(_snap(), wanted)).stored.question
    [call] = seen
    assert (call["a"].label, call["a"].query, call["a"].sublabel) == ("Tteokbokki take 1", "tteokbokki 1", "떡볶이 1")
    assert (call["a"].wiki_lang, call["a"].wiki_title, call["b"].wiki_title) == ("ko", "떡볶이", "호떡")
    assert call["a"].prefer == ("korea", "korean", "떡볶이 1")
    assert "South Korea" in call["context"] and "Frame number 1 here:" in call["context"]
    assert call["hurry"] is wanted  # a waiting player shortens the photo search
    assert (q.option_a.image, q.option_b.image) == (photo_a, photo_b)


@pytest.mark.parametrize(
    "result",
    [
        (OptionImage(url="https://img.example/a.jpg", credit="c"), None),  # lopsided
        (OptionImage(url="https://img.example/a.jpg", credit="c"), OptionImage(url="https://img.example/a.jpg", credit="c")),
        RuntimeError("boom"),  # find_pair shouldn't raise, but a photo is never worth a failed round
    ],
)
async def test_photo_problems_fall_back_to_gradients_on_both_cards(scripted, monkeypatch, result):
    async def find_pair(a, b, **kw):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(images, "find_pair", find_pair)
    q = (await quiz._generate(_snap())).stored.question
    assert q.option_a.image is None and q.option_b.image is None


async def test_photos_shown_earlier_in_the_game_are_excluded(scripted, fake_images):
    rec = seed_session(answered=2, started=False)
    shown = OptionImage(url="https://img.example/earlier.jpg", credit="c", source_url=None)
    live = store.require_session(rec.id)
    live.answers[0].question.option_a.image = shown
    store.save_session(live)
    await quiz._generate(quiz._Snapshot.of(store.require_session(rec.id)))
    assert fake_images.excludes and all(shown.url in ex for ex in fake_images.excludes)


async def test_slow_images_fall_back_to_none(scripted, monkeypatch):
    monkeypatch.setattr(quiz, "IMAGE_BUDGET_S", 0.05)

    async def slow(a, b, **kw):
        await asyncio.sleep(5)

    monkeypatch.setattr(images, "find_pair", slow)
    t0 = time.perf_counter()
    q = (await quiz._generate(_snap())).stored.question
    assert time.perf_counter() - t0 < 1
    assert q.option_a.image is None and q.option_b.image is None


async def test_hedge_wins_when_first_attempt_stalls(scripted, fake_images, monkeypatch):
    monkeypatch.setattr(quiz, "HEDGE_AFTER_S", 0.05)
    first_cancelled = asyncio.Event()
    calls = 0

    async def behavior(user, schema, kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                first_cancelled.set()
                raise
        return payload(1, hypothesis="from hedge")

    scripted.behavior = behavior
    generated = await quiz._generate(_snap())
    assert generated.hypothesis == "from hedge"
    await asyncio.wait_for(first_cancelled.wait(), 1)


async def test_failed_attempt_is_retried_once_then_raises(scripted, fake_images):
    scripted.behavior = lambda user, schema, kw: LLMError("boom")
    with pytest.raises(LLMError):
        await quiz._generate(_snap())
    assert len(scripted.calls) == 2  # original + one retry/hedge


async def test_effort_falls_back_only_when_rejected(scripted, fake_images):
    request = httpx.Request("POST", "https://gateway.example/v1/chat/completions")
    rejected = openai.BadRequestError(
        "Unsupported value: 'reasoning_effort' does not support 'none'",
        response=httpx.Response(400, request=request), body=None,
    )

    def behavior(user, schema, kw):
        if kw["reasoning_effort"] == "none":
            err = LLMError()
            err.__cause__ = rejected
            return err
        return payload(1)

    scripted.behavior = behavior
    await quiz._generate(_snap())
    assert [c.kwargs["reasoning_effort"] for c in scripted.calls] == ["none", "low"]
    assert quiz._effort["value"] == "low"


# --------------------------------------------------------------------------- session flows


async def test_start_quiz_generates_round_one_and_hides_internal_fields(scripted, photos):
    rec = seed_session(started=False)
    q = await quiz.start_quiz(rec.id)
    assert (q.index, q.total, q.depth) == (1, QUIZ_TOTAL, "broad")
    assert q.option_a.image.url == "https://img.example/tteokbokki-1.jpg"
    assert q.option_b.image.url == "https://img.example/hotteok-1.jpg"
    assert sorted(photos[:2]) == ["hotteok 1", "tteokbokki 1"]

    saved = store.require_session(rec.id)
    assert saved.current.question == q
    assert saved.current.dimension == "axis number 1"
    assert saved.hypothesis == "after - at round 1"
    assert saved.extra[quiz.LOCAL_REFS_KEY][q.id] == "Tteokbokki alley 1"
    public = saved.to_state().model_dump_json()
    assert "axis number" not in public and "after - at round" not in public and "alley" not in public

    user = scripted.calls[0].user
    assert "Country: South Korea (KR)" in user and "Round 1 of 10" in user and "(no answers yet)" in user


async def test_start_quiz_is_idempotent_and_dedupes_concurrent_calls(scripted, fake_images):
    rec = seed_session(started=False)
    first, second = await asyncio.gather(quiz.start_quiz(rec.id), quiz.start_quiz(rec.id))
    again = await quiz.start_quiz(rec.id)
    assert first == second == again
    await settle()
    assert scripted.rounds()[1] == 1  # one round-1 generation despite three calls


async def test_start_quiz_after_done_is_conflict(scripted):
    rec = seed_session(done=True)
    with pytest.raises(ConflictError):
        await quiz.start_quiz(rec.id)


async def test_prefetch_both_branches_and_answer_uses_matching_one(scripted, fake_images):
    rec = seed_session(started=False)
    q1 = await quiz.start_quiz(rec.id)
    assert sorted(k[2] for k in quiz.prefetch_keys() if k[1] == q1.id) == ["A", "B"]
    await settle()
    assert scripted.rounds()[2] == 2  # round 2 generated for A and for B

    result = await quiz.answer(rec.id, Answer(question_id=q1.id, choice="B"))
    assert result.done is False and result.next.index == 2
    assert scripted.rounds()[2] == 2  # no fresh generation: the prefetched B branch was used
    saved = store.require_session(rec.id)
    assert saved.hypothesis == "after B at round 2"
    assert saved.current.question == result.next
    assert [(a.question.id, a.choice, a.dimension) for a in saved.answers] == [(q1.id, "B", "axis number 1")]
    assert not any(k[1] == q1.id for k in quiz.prefetch_keys())  # old branches gone
    assert sorted(k[2] for k in quiz.prefetch_keys() if k[1] == result.next.id) == ["A", "B"]


async def test_answer_cancels_the_losing_branch(scripted, fake_images):
    release_a = asyncio.Event()
    b_cancelled = asyncio.Event()

    async def behavior(user, schema, kw):
        if round_of(user) == 2:
            if last_pick(user) == "B":
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    b_cancelled.set()
                    raise
            await release_a.wait()
        return payload(round_of(user), last_pick(user))

    scripted.behavior = behavior
    rec = seed_session(started=False)
    q1 = await quiz.start_quiz(rec.id)
    pending = asyncio.ensure_future(quiz.answer(rec.id, Answer(question_id=q1.id, choice="A")))
    await asyncio.wait_for(b_cancelled.wait(), 1)  # B cancelled as soon as A was picked
    release_a.set()
    result = await pending
    assert result.next.index == 2 and store.require_session(rec.id).hypothesis == "after A at round 2"


async def test_answer_generates_fresh_after_restart(scripted, fake_images):
    rec = seed_session(started=False)
    q1 = await quiz.start_quiz(rec.id)
    await quiz.shutdown()  # simulates a process restart: the in-memory registry is gone
    assert quiz.prefetch_keys() == []
    result = await quiz.answer(rec.id, Answer(question_id=q1.id, choice="A"))
    assert result.next.index == 2
    assert store.require_session(rec.id).hypothesis == "after A at round 2"


async def test_answer_falls_back_when_prefetch_failed(scripted, fake_images):
    failures = {"left": 2}

    def behavior(user, schema, kw):
        if round_of(user) == 2 and failures["left"] > 0:
            failures["left"] -= 1
            return LLMError("prefetch boom")
        return payload(round_of(user), last_pick(user))

    scripted.behavior = behavior
    rec = seed_session(started=False)
    q1 = await quiz.start_quiz(rec.id)
    await settle()
    result = await quiz.answer(rec.id, Answer(question_id=q1.id, choice="A"))
    assert result.next.index == 2


async def test_llm_failure_leaves_session_unchanged_and_retry_works(scripted, fake_images):
    rec = seed_session(started=False)
    q1 = await quiz.start_quiz(rec.id)
    await quiz.shutdown()
    scripted.behavior = lambda user, schema, kw: LLMError("down") if round_of(user) == 2 else payload(round_of(user))
    with pytest.raises(LLMError):
        await quiz.answer(rec.id, Answer(question_id=q1.id, choice="A"))
    saved = store.require_session(rec.id)
    assert saved.current.question.id == q1.id and saved.answers == []

    scripted.behavior = ScriptedLLM.default
    result = await quiz.answer(rec.id, Answer(question_id=q1.id, choice="A"))
    assert result.next.index == 2


async def test_stale_duplicate_and_conflicting_answers(scripted, fake_images):
    rec = seed_session(started=False)
    q1 = await quiz.start_quiz(rec.id)
    with pytest.raises(ConflictError):
        await quiz.answer(rec.id, Answer(question_id="stale", choice="A"))

    first = await quiz.answer(rec.id, Answer(question_id=q1.id, choice="A"))
    replay = await quiz.answer(rec.id, Answer(question_id=q1.id, choice="A"))  # double tap / retry
    assert replay == first
    with pytest.raises(ConflictError):
        await quiz.answer(rec.id, Answer(question_id=q1.id, choice="B"))
    assert len(store.require_session(rec.id).answers) == 1


async def test_concurrent_double_submit_records_one_answer(scripted, fake_images):
    rec = seed_session(started=False)
    q1 = await quiz.start_quiz(rec.id)
    a, b = await asyncio.gather(
        quiz.answer(rec.id, Answer(question_id=q1.id, choice="A")),
        quiz.answer(rec.id, Answer(question_id=q1.id, choice="A")),
    )
    assert a == b
    assert len(store.require_session(rec.id).answers) == 1


async def test_final_round_finishes_and_folds_last_pick_into_hypothesis(scripted, fake_images):
    rec = seed_session(answered=QUIZ_TOTAL - 1, hypothesis="before the end")
    last = rec.current.question
    assert last.index == QUIZ_TOTAL
    assert quiz.prefetch_keys() == []

    result = await quiz.answer(rec.id, Answer(question_id=last.id, choice="B"))
    assert result.next is None and result.done is True
    saved = store.require_session(rec.id)
    assert saved.quiz_done and saved.current is None and len(saved.answers) == QUIZ_TOTAL
    assert saved.answers[-1].choice == "B"
    assert not any(k[0] == rec.id for k in quiz.prefetch_keys())

    await settle()
    assert store.require_session(rec.id).hypothesis == "final hypothesis"
    final_call = scripted.calls[-1]
    assert final_call.schema is quiz.HypothesisUpdate and f"R{QUIZ_TOTAL}" in final_call.user
    with pytest.raises(ConflictError):
        await quiz.start_quiz(rec.id)


async def test_full_game_depth_curve_and_no_task_leaks(scripted, fake_images):
    rec = seed_session(started=False)
    q = await quiz.start_quiz(rec.id)
    depths = [q.depth]
    for i in range(QUIZ_TOTAL):
        result = await quiz.answer(rec.id, Answer(question_id=q.id, choice="AB"[i % 2]))
        if result.done:
            break
        q = result.next
        depths.append(q.depth)
    assert result.done and len(depths) == QUIZ_TOTAL
    assert depths == [quiz.depth_for_round(i) for i in range(1, QUIZ_TOTAL + 1)]
    await settle()
    assert quiz.prefetch_keys() == [] and not quiz._starts and not quiz._background
    saved = store.require_session(rec.id)
    assert len(saved.answers) == QUIZ_TOTAL and saved.quiz_done
    assert len(saved.extra[quiz.LOCAL_REFS_KEY]) == QUIZ_TOTAL


async def test_registry_evicts_stale_and_overflowing_entries(scripted, fake_images, monkeypatch):
    blocker = asyncio.Event()
    tasks = []
    for i in range(4):
        task = asyncio.ensure_future(blocker.wait())
        tasks.append(task)
        # q0 is past the TTL; q1 is the oldest of the rest, so the size cap evicts it next.
        quiz._prefetch[("s", f"q{i}", "A")] = quiz._Prefetch(task, time.monotonic() - (10_000 if i == 0 else 10 - i))
    monkeypatch.setattr(quiz, "PREFETCH_MAX", 2)
    quiz._evict_stale()
    await asyncio.sleep(0)
    assert sorted(k[1] for k in quiz._prefetch) == ["q2", "q3"]
    assert tasks[0].cancelled() and tasks[1].cancelled() and not tasks[2].done()


# --------------------------------------------------------------------------- HTTP integration


def test_api_plays_a_whole_game(client, fake_llm, fake_images):
    def handler(call):
        if call.schema is quiz.HypothesisUpdate:
            return {"hypothesis": "final"}
        return payload(round_of(call.user), last_pick(call.user))

    fake_llm.on("chat_json", handler)
    sid = client.post("/api/session", json={"country": {"code": "BR", "name": "Brazil"}}).json()["id"]
    res = client.post(f"/api/quiz/{sid}/start")
    assert res.status_code == 200, res.text
    q = res.json()
    assert set(q) == {"id", "index", "total", "prompt", "depth", "option_a", "option_b"}
    assert set(q["option_a"]) == {"label", "sublabel", "emoji", "color", "image"}
    assert client.post(f"/api/quiz/{sid}/start").json()["id"] == q["id"]  # idempotent

    for i in range(QUIZ_TOTAL):
        res = client.post(f"/api/quiz/{sid}/answer", json={"question_id": q["id"], "choice": "AB"[i % 2]})
        assert res.status_code == 200, res.text
        body = res.json()
        if body["done"]:
            assert body["next"] is None
            break
        q = body["next"]
        assert q["index"] == i + 2
    state = client.get(f"/api/session/{sid}").json()
    assert state["quiz_done"] is True and len(state["answers"]) == QUIZ_TOTAL
    assert state["current_question"] is None
    assert "dimension" not in str(state) and "hypothesis" not in str(state)
    assert client.post(f"/api/quiz/{sid}/answer", json={"question_id": q["id"], "choice": "A"}).status_code == 409


def test_api_maps_generation_failure_to_502(client, fake_llm):
    fake_llm.on("chat_json", lambda call: LLMError("Our AI had a hiccup. Please try again."))
    rec = seed_session(started=False)
    res = client.post(f"/api/quiz/{rec.id}/start")
    assert res.status_code == 502
    assert res.json() == {"detail": "Our AI had a hiccup. Please try again."}
    assert store.require_session(rec.id).current is None
