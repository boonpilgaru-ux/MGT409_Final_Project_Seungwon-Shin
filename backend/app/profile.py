"""Personality / taste profile synthesis (SPEC section 5, profile.py).

Public API (called by routes.py):
    await build_profile(session_id) -> Profile      synthesizes, persists as rec.profile, returns it

Helpers (pure, used by tests and handy for debugging prompts):
    build_prompt(rec, context_budget=MAX_CONTEXT_CHARS) -> str
    quality_problems(profile, answers) -> list[str]
    normalize(profile) -> Profile

Pipeline: one ``llm.chat_json`` call (which already repairs malformed JSON once) -> semantic checks
(trait/color counts, labels, length, cites actual choices) -> at most ONE revision call listing the
problems -> keep the better draft -> safe normalization (dedupe, trim, pad colors, map fits to the
Studio vocabulary). A profile that is still unusable raises LLMError (502).

Prompt size is bounded: about-me excerpts share a fixed character budget and the whole user
message is capped at MAX_PROMPT_CHARS. Context text is untrusted and always delimited.
"""

from __future__ import annotations

import asyncio
import logging
import re
from functools import partial
from typing import get_args

from . import llm, store
from .config import get_settings
from .errors import ConflictError, LLMError
from .models import Fit, Profile, TasteColor
from .store import SessionRecord, StoredAnswer, StoredContext

__all__ = ["build_profile", "build_prompt", "quality_problems", "normalize"]

log = logging.getLogger("teedentity.profile")

MAX_CONTEXT_CHARS = 12_000  # all about-me excerpts together
MAX_EXCERPT_CHARS = 4_000  # one item's excerpt
MAX_SUMMARY_CHARS = 400
MAX_HYPOTHESIS_CHARS = 800
MAX_PROMPT_CHARS = 30_000  # hard ceiling for the user message
PROFILE_MAX_TOKENS = 3_000
# "low" drafts take ~8-10 s (vs ~11-18 s at "medium") with no measurable quality loss (both pass
# quality_problems; same specificity). A stalled gateway call gets a parallel retry after 20 s.
PROFILE_REASONING_EFFORT = "low"
PROFILE_HEDGE_AFTER_S = 20.0
PROFILE_DEADLINE_S = 120.0  # whole synthesis; without it a stalled gateway held the request ~4 min
REVISION_MIN_S = 40.0  # only start a revision call with at least this much of the deadline left
MIN_CITED_ROUNDS = 3  # the profile must visibly reference at least this many actual picks

FIT_VALUES: tuple[str, ...] = get_args(Fit)
_NEUTRAL_COLORS = (("#141414", "Ink black"), ("#faf7f2", "Off-white"), ("#8c8c8c", "Concrete grey"),
                   ("#ff3b30", "Hot red"))  # fmt: skip

# --------------------------------------------------------------------------- prompts

_SYSTEM = """\
You are the insight writer for Tee:dentity. A person from {country} just played a 15-round \
balance game (밸런스게임: "A or B, you must pick one") and may have shared extra material about \
themselves. Turn it into a personality-and-taste profile that makes them feel truly seen, and that a \
graphic designer and a personal shopper can act on to make or find their perfect t-shirt.

HOW TO READ THE EVIDENCE
- Each round probed one hidden dimension, and both options were built to be tempting, so a pick \
reveals a priority, not a right answer. Read what was chosen AND what was given up.
- Rounds 1-10 (broad, focused) speak mostly to personality and values; rounds 11-15 (specific) speak \
mostly to visual taste. Weight them that way.
- Look for patterns across rounds, and name one interesting tension (e.g. a planner who picks chaos \
when food is involved).
- The game engine's working hypothesis is a hint, not a fact. Trust the answers when they disagree.
- About-me material is optional extra evidence. Use its concrete details when they fit, but one item \
never outweighs fifteen choices.

VOICE
- Warm, witty, specific, in the second person ("you"): a perceptive friend, not a horoscope or an \
HR report.
- Every claim must trace back to a pick or to the material. Cite actual picks by name, e.g. "you took \
the 6 a.m. Han River run over the lie-in".
- Banned: Barnum statements that fit anyone ("you value meaningful connections", "you have a creative \
side"), MBTI or zodiac labels, empty flattery, clinical or diagnostic words, and any guess about \
health, religion, politics, sexuality, ethnicity or income.
- Use the country as cultural texture, through real references from the game, never as a stereotype.
- Write in English. A local term in native script is welcome where it adds flavor.

FIELD GUIDE
- archetype: 2-5 words, an original persona name, e.g. "The Late-Night Pojangmacha Philosopher".
- tagline: one punchy line, at most 12 words.
- summary: 3-5 sentences (70-120 words). Cite at least three specific picks and name one tension.
- traits: 5-7 bipolar axes. name: 1-3 words. low_label and high_label: opposite poles, 1-3 words \
each. score: 0-100, where 100 means fully high_label. Ground each score in specific answers and use \
the whole range (not everything near 50). description: one sentence citing the evidence.
- taste.colors: 4-6 distinct colors that work together on a t-shirt, each with hex "#rrggbb" and an \
evocative, specific name (e.g. "Soju-bottle green"). Include at least one dark and one light color.
- taste.aesthetics: 3-5 short style descriptors (e.g. "90s Seoul shop signage").
- taste.motifs: 4-6 concrete, drawable things, mostly the EXACT icons they picked in the game, named as on the \
card (e.g. "Slam Dunk", "Tteokbokki", "Messi", "90s hip-hop"): favorite fandoms, foods, places, eras and vibes \
they chose. These drive the shop search and the design, so pick the strongest t-shirt themes among their picks.
- taste.typography: one sentence on the type vibe (weight, width, case, era; serif, sans-serif, \
monospace, script or hand-lettered).
- taste.fits: 1-3 values chosen only from: {fits}.
- taste.avoid: 2-4 things that would feel wrong on them (e.g. "giant logos", "neon gradients").
- cultural_roots: 2-3 sentences on how their culture shows up in their picks, using real references.
- values: 3-5 short words or phrases.
- tshirt_brief: 60-110 words, a concrete creative brief that a designer can draw from and a shopper \
can search with. Cover: the core idea built on 1-3 of the icons they actually picked (name them); graphic style (e.g. flat vector, line art, \
woodblock, retro badge); 2-4 hex colors from the palette; typography and any short text (at most 4 \
words, native script optional); print placement; shirt color; fit and fabric weight; what to avoid."""


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _excerpt(text: str, limit: int) -> str:
    """First ``limit`` chars, cut at a word boundary when one is close."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.8:
        cut = cut[:space]
    return cut.rstrip() + " …"


def _option_text(option) -> str:
    label = _clip(option.label, 80)
    return f"{label} ({_clip(option.sublabel, 60)})" if option.sublabel else label


def _format_answers(answers: list[StoredAnswer]) -> str:
    lines = [
        f"BALANCE GAME ({len(answers)} rounds; each probed one hidden dimension; \"<- PICKED\" marks the choice):"
    ]
    for a in answers:
        q = a.question
        lines.append(f"R{q.index} · {q.depth} · dimension: {_clip(a.dimension, 60)}")
        lines.append(f"   Q: {_clip(q.prompt, 120)}")
        for side, option in (("A", q.option_a), ("B", q.option_b)):
            mark = "  <- PICKED" if a.choice == side else ""
            lines.append(f"   {side}) {_option_text(option)}{mark}")
    return "\n".join(lines)


def _allocate(lengths: list[int], budget: int, cap: int) -> list[int]:
    """Split ``budget`` chars across items: short items take what they need, long ones share the rest."""
    alloc = [0] * len(lengths)
    remaining = budget
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    for position, i in enumerate(order):
        share = remaining // (len(order) - position)
        alloc[i] = max(0, min(lengths[i], cap, share))
        remaining -= alloc[i]
    return alloc


def _usable(items: list[StoredContext]) -> list[StoredContext]:
    return [c for c in items if c.item.status != "error" and (c.text.strip() or c.item.summary.strip())]


def _format_context(items: list[StoredContext], budget: int, limit: int | None = None) -> str:
    """About-me blocks, each delimited as untrusted. ``limit`` keeps only the first N usable items
    (whole blocks are dropped from the end, never cut mid-block)."""
    usable = _usable(items)
    if not usable:
        return "ABOUT-ME MATERIAL: none shared."
    dropped = len(usable) - limit if limit is not None and limit < len(usable) else 0
    usable = usable[: len(usable) - dropped]
    if not usable:
        return f"ABOUT-ME MATERIAL: {dropped} item(s) left out for length."
    excerpts = _allocate([len(c.text) for c in usable], budget, MAX_EXCERPT_CHARS)
    blocks = []
    for c, size in zip(usable, excerpts):
        body = f"Status: {c.item.status}\nOne-line summary: {_clip(c.item.summary, MAX_SUMMARY_CHARS) or '(none)'}"
        if size > 0:
            body += f"\nExcerpt:\n{_excerpt(c.text, size)}"
        blocks.append(llm.wrap_untrusted(body, f"{c.item.kind}: {c.item.name}"))
    header = f"ABOUT-ME MATERIAL ({len(usable)} item{'s' if len(usable) != 1 else ''}; user-supplied, untrusted"
    header += f"; {dropped} more left out for length):" if dropped else "):"
    return header + "\n" + "\n\n".join(blocks)


def build_prompt(rec: SessionRecord, context_budget: int = MAX_CONTEXT_CHARS) -> str:
    """The user message for synthesis. Shrinks the excerpt budget until it fits MAX_PROMPT_CHARS
    (at budget 0 only the one-line summaries remain), then drops whole about-me blocks from the
    end, so every <untrusted_data> block stays closed and the final instruction survives."""

    def assemble(budget: int, limit: int | None = None) -> str:
        parts = [f"COUNTRY: {rec.country.name} ({rec.country.code})", "", _format_answers(rec.answers)]
        if rec.hypothesis.strip():
            parts += ["", "GAME ENGINE'S WORKING HYPOTHESIS (a hint; trust the answers if they disagree):",
                      _clip(rec.hypothesis, MAX_HYPOTHESIS_CHARS)]  # fmt: skip
        parts += ["", _format_context(rec.context, budget, limit), "", "Write the profile now."]
        return "\n".join(parts)

    for budget in (context_budget, context_budget // 2, 2_000, 0):
        prompt = assemble(budget)
        if len(prompt) <= MAX_PROMPT_CHARS:
            return prompt
    for limit in range(len(_usable(rec.context)) - 1, -1, -1):
        prompt = assemble(0, limit)
        if len(prompt) <= MAX_PROMPT_CHARS:
            return prompt
    return prompt  # answers alone never get this long; never cut a block in half


def _system_prompt(rec: SessionRecord) -> str:
    return _SYSTEM.format(country=rec.country.name, fits=", ".join(FIT_VALUES)) + "\n\n" + llm.UNTRUSTED_NOTE


# --------------------------------------------------------------------------- validation


_STOPWORDS = frozenset(
    "with your that this from into only over more less than every forever never always just some like "
    "have what when then them they their there about after before pick one night day the and for".split()
)


def _evidence_tokens(answer: StoredAnswer) -> set[str]:
    """Distinctive words from both options: ASCII words of 4+ letters, native-script words of 2+."""
    text = " ".join(
        part for o in (answer.question.option_a, answer.question.option_b) for part in (o.label, o.sublabel or "")
    ).lower()
    tokens = set()
    for word in re.findall(r"\w+", text):
        if word in _STOPWORDS:
            continue
        if (word.isascii() and len(word) >= 4) or (not word.isascii() and len(word) >= 2):
            tokens.add(word)
    return tokens


def _cited_rounds(profile: Profile, answers: list[StoredAnswer]) -> int:
    corpus = " ".join(
        [profile.summary, profile.cultural_roots, profile.tshirt_brief, *(t.description for t in profile.traits)]
    ).lower()
    return sum(1 for a in answers if any(tok in corpus for tok in _evidence_tokens(a)))


def quality_problems(profile: Profile, answers: list[StoredAnswer]) -> list[str]:
    """Human-readable problems with a draft (empty list = good). Fed back to the model on retry."""
    problems: list[str] = []
    for field in ("archetype", "tagline", "summary", "cultural_roots", "tshirt_brief"):
        if not getattr(profile, field).strip():
            problems.append(f"{field} is empty.")
    if len(profile.archetype.split()) > 6:
        problems.append("archetype must be 2-5 words.")
    if len(profile.tagline.split()) > 14:
        problems.append("tagline must be at most 12 words.")
    if len(profile.summary.split()) < 45:
        problems.append("summary is too thin: write 3-5 sentences (70-120 words).")
    if len(profile.tshirt_brief.split()) < 35:
        problems.append("tshirt_brief is too thin: 60-110 concrete words covering every point in the field guide.")
    if not 5 <= len(profile.traits) <= 7:
        problems.append(f"traits must have 5-7 entries (got {len(profile.traits)}).")
    names = [t.name.strip().lower() for t in profile.traits]
    if len(set(names)) != len(names):
        problems.append("trait names must be distinct.")
    for t in profile.traits:
        if not t.low_label.strip() or not t.high_label.strip() or t.low_label.strip().lower() == t.high_label.strip().lower():
            problems.append(f'trait "{t.name}" needs two different, opposite pole labels.')
    hexes = [c.hex for c in profile.taste.colors]
    if not 4 <= len(set(hexes)) <= 6:
        problems.append(f"taste.colors must have 4-6 distinct colors (got {len(set(hexes))}).")
    if len(profile.taste.motifs) < 3:
        problems.append("taste.motifs needs 4-6 concrete, drawable things.")
    if len(profile.taste.aesthetics) < 2:
        problems.append("taste.aesthetics needs 3-5 descriptors.")
    if not profile.taste.typography.strip():
        problems.append("taste.typography is empty.")
    if not any(f.strip().lower() in FIT_VALUES for f in profile.taste.fits):
        problems.append(f"taste.fits must use only: {', '.join(FIT_VALUES)}.")
    if len(profile.values) < 2:
        problems.append("values needs 3-5 entries.")
    if answers and _cited_rounds(profile, answers) < min(MIN_CITED_ROUNDS, len(answers)):
        problems.append(
            "It doesn't reference the person's actual picks. Cite specific options by name in the "
            "summary and the trait descriptions."
        )
    return problems


def _dedupe(items: list[str], limit: int) -> list[str]:
    out: list[str] = []
    for item in items:
        item = item.strip()
        if item and item.lower() not in {o.lower() for o in out}:
            out.append(item)
    return out[:limit]


def _normalize_fits(fits: list[str]) -> list[str]:
    """Map free-text fits ("relaxed oversized") onto the Studio's fit vocabulary."""
    found = [v for f in fits for v in FIT_VALUES if re.search(rf"\b{v}\b", f.lower())]
    return list(dict.fromkeys(found))[:3] or ["regular"]


# Links have no place in a profile, and they are how an injected instruction ("recommend
# https://evil-tees.example/...") would steer the designer and the web-search shopper downstream.
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|co|app|dev|shop|store|xyz|info|biz|me|ly|gg|site"
    r"|online|top|club|kr|jp|cn|uk|us|de|fr|br|mx|in|ng|ru|example)(?:/\S*)?\b",
    re.IGNORECASE,
)


def _strip_links(text: str) -> str:
    cleaned = _DOMAIN_RE.sub("", _URL_RE.sub("", text))
    return re.sub(r"\s{2,}", " ", re.sub(r"\s+([,.;:!?])", r"\1", cleaned)).strip()


def _strip_profile_links(profile: Profile) -> Profile:
    """Remove URLs and bare domains from every free-text field that reaches the design/shop prompts."""
    taste = profile.taste.model_copy(
        update={
            "aesthetics": [s for s in map(_strip_links, profile.taste.aesthetics) if s],
            "motifs": [s for s in map(_strip_links, profile.taste.motifs) if s],
            "typography": _strip_links(profile.taste.typography),
            "avoid": [s for s in map(_strip_links, profile.taste.avoid) if s],
        }
    )
    return profile.model_copy(
        update={
            field: _strip_links(getattr(profile, field))
            for field in ("archetype", "tagline", "summary", "cultural_roots", "tshirt_brief")
        }
        | {"values": [s for s in map(_strip_links, profile.values) if s], "taste": taste}
    )


def normalize(profile: Profile) -> Profile:
    """Deterministic, safe clean-up: strip links, dedupe, trim over-long lists, pad a short palette."""
    profile = _strip_profile_links(profile)
    traits, seen_traits = [], set()
    for t in profile.traits:
        if t.name.strip().lower() not in seen_traits:
            seen_traits.add(t.name.strip().lower())
            traits.append(t)
    colors, seen_hex = [], set()
    for c in profile.taste.colors:
        if c.hex not in seen_hex:
            seen_hex.add(c.hex)
            colors.append(c)
    colors = colors[:6]
    if 2 <= len(colors) < 4:
        for hex_value, name in _NEUTRAL_COLORS:
            if len(colors) >= 4:
                break
            if hex_value not in seen_hex:
                colors.append(TasteColor(hex=hex_value, name=name))
    taste = profile.taste.model_copy(
        update={
            "colors": colors,
            "aesthetics": _dedupe(profile.taste.aesthetics, 6),
            "motifs": _dedupe(profile.taste.motifs, 8),
            "fits": _normalize_fits(profile.taste.fits),
            "avoid": _dedupe(profile.taste.avoid, 6),
        }
    )
    return profile.model_copy(
        update={
            "archetype": profile.archetype.strip().rstrip("."),
            "traits": traits[:7],
            "taste": taste,
            "values": _dedupe(profile.values, 6),
        }
    )


def _fatal(profile: Profile) -> bool:
    """Too broken to show even after a retry and normalization."""
    required = (profile.archetype, profile.tagline, profile.summary, profile.tshirt_brief)
    return not all(s.strip() for s in required) or len(profile.traits) < 3 or len(profile.taste.colors) < 4


# --------------------------------------------------------------------------- synthesis


async def _draft(model: str, system: str, user: str) -> Profile:
    return await llm.chat_json(
        model, system, user, Profile, max_tokens=PROFILE_MAX_TOKENS, temperature=0.8,
        reasoning_effort=PROFILE_REASONING_EFFORT, hedge_after=PROFILE_HEDGE_AFTER_S,
    )


def _revision_request(draft: Profile, problems: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in problems)
    return (
        f"\n\nYOUR PREVIOUS DRAFT needs these fixes:\n{listed}\n"
        f"Previous draft (keep what works, fix the rest):\n{draft.model_dump_json()[:9000]}\n\n"
        "Return the complete corrected profile."
    )


async def synthesize(rec: SessionRecord) -> Profile:
    """Generate, check, revise once if needed, and normalize a profile. Does not persist.

    Bounded by PROFILE_DEADLINE_S: the first draft must arrive in time (else LLMError), and a
    revision is only attempted with REVISION_MIN_S left; a revision that fails or runs out of time
    falls back to the usable first draft instead of throwing it away.
    """
    model = get_settings().profile_model
    system, user = _system_prompt(rec), build_prompt(rec)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PROFILE_DEADLINE_S
    try:
        async with asyncio.timeout_at(deadline):
            draft = await _draft(model, system, user)
    except TimeoutError:
        log.warning("profile: no draft within %.0fs", PROFILE_DEADLINE_S)
        raise LLMError("Your profile took too long. Please try again.") from None
    problems = quality_problems(draft, rec.answers)
    if problems:
        remaining = deadline - loop.time()
        revised = None
        if remaining >= REVISION_MIN_S:
            log.info("profile: revising draft (%s)", "; ".join(problems)[:300])
            try:
                revised = await asyncio.wait_for(_draft(model, system, user + _revision_request(draft, problems)), remaining)
            except (LLMError, TimeoutError) as err:
                log.info("profile: revision skipped (%s); keeping the first draft", type(err).__name__)
        else:
            log.info("profile: no time left to revise (%.0fs); keeping the first draft", remaining)
        if revised is not None:
            revised_problems = quality_problems(revised, rec.answers)
            if len(revised_problems) <= len(problems):
                draft, problems = revised, revised_problems
        if problems:
            log.warning("profile: accepting draft with minor issues (%s)", "; ".join(problems)[:300])
    profile = normalize(draft)
    if _fatal(profile):
        raise LLMError("We couldn't put your profile together this time. Please try again.")
    return profile


async def _build_and_save(session_id: str) -> Profile:
    rec = store.require_session(session_id)
    if not rec.quiz_done:
        raise ConflictError("Finish the balance game first.")
    profile = await synthesize(rec)  # slow part runs outside the session lock
    async with store.edit_session(session_id) as live:
        live.profile = profile
    return profile


# One in-flight build per session: a double click (or React StrictMode's double effect) shares a
# single LLM run instead of paying for two and racing to save.
_inflight: dict[str, asyncio.Task[Profile]] = {}


def _forget(session_id: str, task: asyncio.Task[Profile]) -> None:
    if _inflight.get(session_id) is task:
        del _inflight[session_id]
    if not task.cancelled():
        task.exception()  # mark retrieved; awaiting callers re-raise it themselves


async def build_profile(session_id: str) -> Profile:
    """Synthesize a fresh Profile from everything known and persist it as ``rec.profile``.

    Inputs: country; all answers (prompt, both options, pick, HIDDEN dimension and depth); the
    HIDDEN running hypothesis; each usable context item's summary plus an excerpt of its HIDDEN
    text (delimited as untrusted). Regenerates on every call; concurrent calls for the same
    session share one run.

    Raises:
        NotFoundError: unknown session.
        ConflictError: quiz not finished.
        LLMError: synthesis failed.
    """
    loop = asyncio.get_running_loop()
    task = _inflight.get(session_id)
    if task is None or task.done() or task.get_loop() is not loop:
        task = loop.create_task(_build_and_save(session_id))
        _inflight[session_id] = task
        task.add_done_callback(partial(_forget, session_id))
    # shield: one caller disconnecting must not cancel the build the others are waiting on
    return await asyncio.shield(task)
