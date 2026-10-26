"""Option photos for balance-game cards (SPEC section 3, "Image lookup").

``await images.find_pair(PhotoSpec(...), PhotoSpec(...), exclude=urls, context=...)`` ->
``(OptionImage | None, OptionImage | None)``: both cards get a photo, or neither does. Never raises
(except ``CancelledError``).

Pipeline per round (both cards together):
  1. Candidates per card, all fetched concurrently (cached per article / query, concurrent callers
     share one request):
     Tier 1  the lead image of the Wikipedia article the question model named for the card
             (``wiki_lang`` / ``wiki_title``, e.g. ko / 빈대떡): exact title (redirects followed),
             else that wiki's search for the title (it tolerates fuzzy titles), and if the article
             has no free lead image, its English counterpart. Disambiguation pages are skipped, and
             so are logos, maps, flags, coats of arms, diagrams and SVGs unless the card IS one.
             The file's own description and categories are fetched for the judge (and the credit).
     Tier 2  Wikimedia Commons search with ``image_query``, filtered as before: jpg/png/webp,
             >= 400 px, not a panorama, no blocklisted words, and *relevant* (the query's first
             word, its head noun and its proper nouns must match; see ``_relevance``).
     Openverse was dropped (measured in five live games: 70 of 87 rounds hit its 4 s timeout,
     its anonymous quota is 200 requests a day, and 7 of its 17 photos were off-topic or showed
     identifiable private people).
  2. ONE judge call (the course model, fixed to ``gpt-6-luna``) for every candidate of both
     cards: it reads titles, descriptions and categories in any language and marks each "yes"
     (shows the card's thing), "partial", "no" or "unsafe" (war, weapons, military, atrocities,
     slavery, disasters, memorials/graves, politicians/royals/religious figures or rituals,
     a private person as the subject, nudity, extremist symbols, drugs).
  3. Each card takes its first "yes" in tier order, never a photo already shown in the game
     (``exclude``) or the other card's photo. Parity: a card with no photo gets its next tier
     (narrower Commons queries, its native-script name, a Wikipedia search for its photo query),
     judged once more; only if it still has nothing do both cards use the gradient fallback.
     The extra tier is skipped once ``hurry`` is set (a player is waiting for the round).
  If the judge fails, only tier-1 lead images that pass the word blocklist are used.

Wikimedia rejects (403) User-Agents without a URL or email. The default UA therefore carries the
app's URL (``TEE_PUBLIC_URL``, default the local dev URL); ``TEE_USER_AGENT`` overrides the whole
string. Never put a personal email in it.
"""

from __future__ import annotations

import asyncio
import difflib
import html
import logging
import os
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from . import llm
from .config import COURSE_MODEL, PUBLIC_URL
from .errors import LLMError
from .models import OptionImage

__all__ = ["USER_AGENT", "JUDGE_MODEL", "PhotoSpec", "find_pair", "clear_cache", "aclose"]

log = logging.getLogger("teedentity.images")

USER_AGENT = os.getenv("TEE_USER_AGENT", "").strip() or f"Teedentity/0.1 (+{PUBLIC_URL}; student project)"
# Benchmarked on 62 labelled candidates (sensitive ones described in 6 languages): see SPEC section 3.
JUDGE_MODEL = COURSE_MODEL

COMMONS_API = "https://commons.wikimedia.org/w/api.php"

REQUEST_TIMEOUT_S = 3.0  # one Wikipedia / Commons API request (they normally answer in 0.2-0.8 s)
GATHER_S = 2.0  # the judge starts with whatever candidates arrived by then (stragglers still fill the cache)
HURRIED_GATHER_S = 1.0  # the next tier's window while a player is waiting for the round
JUDGE_TIMEOUT_S = 5.0
JUDGE_HEDGE_S = 2.5  # a slow judge call gets one parallel duplicate (gateway latency has a long tail)
JUDGE_MAX_TOKENS = 300
MAX_CONCURRENT_REQUESTS = 4  # Wikimedia asks clients for at most ~3 concurrent requests
REQUESTS_PER_MIN = 180.0  # our Wikimedia request budget (its limit for identified bots: ~200/min)
REQUEST_BURST = 24.0
MAX_QUEUE_S = 1.5  # a request that would wait longer for budget is skipped (that card may fall back)
BACKOFF_S = 60.0  # pause after a 429 without a Retry-After
MIN_WIDTH = 400
MIN_HEIGHT = 300
MIN_SQUARENESS = 0.45  # min(w/h, h/w); cards are 1:1 "cover", so panoramas crop badly
MIN_RELEVANCE = 0.5  # share of the query's significant words found in a Commons candidate's text
HEAD_OPTIONAL_SHARE = 0.75  # below this share, the query's head noun (last word) must match
COUNTRY_BONUS = 0.6  # a photo that names the player's country beats a same-named place elsewhere
THUMB_WIDTH = 960  # a standard Wikimedia thumbnail step (non-standard sizes are remapped or refused)
COMMONS_PER_CARD = 3  # best Commons candidates per query shown to the judge
SEARCH_PER_CARD = 2  # Wikipedia search hits per card shown to the judge
JUDGE_PER_CARD = 6  # candidates per card in one judge call
CACHE_SIZE = 512
HIT_TTL_S = 24 * 3600.0
MISS_TTL_S = 5 * 60.0

_ALLOWED_EXT = {"jpg", "jpeg", "png", "webp"}
_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
_LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z]{2,8})?$|^simple$")

# Grammar words never count toward relevance; generic "framing" words are dropped from the
# first fallback query too ("bowl of jjajangmyeon" -> "jjajangmyeon").
_STOPWORDS = {
    "a", "an", "the", "of", "with", "and", "or", "in", "on", "at", "for", "to", "from", "by", "into",
    "over", "under", "near", "its", "their", "my", "your", "some", "very", "full", "s",
}
_GENERIC = {
    "photo", "picture", "image", "view", "closeup", "close", "up", "shot", "scene", "bowl", "plate",
    "cup", "mug", "glass", "piece", "slice", "pile", "stack", "pair", "set", "kind", "type", "style",
}

# Lead images that aren't photos of the thing: skipped unless the card itself is one of these
# (a card about a team crest may show a crest). Matched on the folded file name.
_NON_PHOTO_RE = re.compile(
    r"\b(?:logo\w*|wordmark|flags?|coat of arms|coats of arms|emblem|seal|crest|insignia|maps?|locator|"
    r"location map|diagram|chart|graph|icon|symbol|signature|blank|plan|schematic|svg)\b"
)
_NON_PHOTO_CARD_RE = re.compile(
    r"\b(?:logo\w*|wordmark|flags?|emblem|seal|crest|badge|insignia|maps?|diagram|chart|icon|symbol)\b"
)

# Tests may inject an ``httpx.MockTransport`` here.
_transport: httpx.AsyncBaseTransport | None = None

# Cheap first gate before the judge (and its stand-in when the judge is down): photos the balance
# game must never show. Matched on the folded title + description + categories of every candidate.
# The LLM judge reads every language; this list only knows English and a few Spanish / Hindi site
# words. Harmless compounds are carved out ("emperor penguin", "Swiss Army knife", "sugar skull",
# "riot grrrl", "Temple Street night market", "Shirley Temple", "fire brigade", "Air Force 1"
# sneakers, "Chiba Lotte Marines", "Islamabad", "monarch butterfly"); "crush" only counts as "crowd
# crush", "church" never matches "Churchill", a bare "navy" is the colour, and only a "battle tank"
# is military ("tank top", "fish tank" are fine).
_SENSITIVE_RE = re.compile(
    r"\b(?:nazi\w*|swastikas?|luftwaffe|wehrmacht|waffen|third reich|ww ?(?:i{1,2}|1|2)|world wars?|holocaust|"
    r"genocide|memorials?|victims?|massacres?|disasters?|traged\w*|crowd crush|stampede|funerals?|cemeter\w*|"
    r"graveyards?|gravestones?|tombstones?|coffins?|corpses?|burials?|(?<!sugar )skulls?|executions?|beggars?|homeless\w*|"
    r"emperors?(?! penguin)|presidents?|prime ministers?|minist(?:er|ers|ry|ries)|politicians?|parliament\w*|"
    r"senators?|elections?|campaign\w*|protest\w*|riots?(?! grrrl)|militar\w*|(?<!swiss )army|armies|soldiers?|"
    r"troops|weapons?|rifles?|police|signing ceremony|memorandum|delegations?|"
    r"naval|navy (?:reserve|seals?|ships?|base|officers?)|(?:us|u s|royal) navy|airborne|marine corps|"
    r"(?<!lotte )marines|infantry|regiments?|battalions?|(?<!fire )brigades?|insignia|dvids|veterans?|"
    r"air force(?! (?:1|one|ones)\b)|coast guard|national guard|warships?|battleships?|"
    r"battle tanks?|merkava|artillery|howitzers?|missiles?|armou?red vehicles?|slave\w*|"
    r"government\w*|royal family|prince(?:ss)? of wales|crown prince\w*|empress\w*|monarchs?(?! butterfl)|monarch(?:y|ies)|"
    r"hanukkah|chanukah|menorah|dasara|dussehra|navaratri|durga|"
    r"religio\w*|gods?|deity|deities|church(?:es)?|mosques?|altars?|bible|quran|jesus|allah|buddhas?|"
    r"prayers?|worship\w*|pope|priests?|monks?|nuns?|monaster(?:y|ies)|pagodas?|stupas?|"
    r"(?<!shirley )temples?(?! (?:street|bar|run))|templos?|shrines?|mandirs?|iglesias?|cathedrals?|catedral|"
    r"chapels?(?! (?:street|hill))|capilla|basilicas?|synagogues?|gurdwaras?|catholic\w*|"
    r"christians?(?! (?:dior|louboutin|lacroix))|christianity|hindu\w*|buddhis\w*|islam(?!abad)\w*|muslims?|"
    r"sikh\w*|krishna|ganesh\w*|shiva|vishnu|puja|pooja|"
    r"efta\d+|epstein|nude\w*|naked|porn\w*|erotic\w*)\b"
)


# --------------------------------------------------------------------------- text helpers


def _fold(text: str) -> str:
    """Lowercase, strip accents, and turn punctuation into spaces ("Pão-de-Queijo" -> "pao de queijo")."""
    decomposed = unicodedata.normalize("NFKD", text)
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[\W_]+", " ", no_marks.lower()).strip()


def _normalize_query(query: str) -> str:
    return " ".join(str(query or "").split())[:120]


def _is_sensitive(title: str, detail: str = "") -> bool:
    return bool(_SENSITIVE_RE.search(_fold(f"{title} {detail}")))


@dataclass(frozen=True)
class _Terms:
    tokens: tuple[str, ...]  # significant words (folded)
    required: frozenset[str]  # proper nouns: must appear in a candidate's title
    head: str  # last significant word, i.e. the subject ("kimbap" in "triangle kimbap"); "" if none
    # Narrower retry queries (the parity tier) with the relevance each must reach.
    fallbacks: tuple[tuple[str, float], ...]
    # Real (mid-query capitalised) proper nouns: matched whole, never by plural stem ("Lagos" is
    # not "Lago").
    proper: frozenset[str] = frozenset()
    # Folded country-name words / demonyms of the player's country, and the scripts of its native
    # names ("HANGUL"): a photo mentioning one, or described in that script, ranks higher
    # ("Seoul-Han.River.at.night", described "여의도동, 서울", over "Han River (Qianjiang)"). Never required.
    prefer: frozenset[str] = frozenset()
    prefer_scripts: frozenset[str] = frozenset()


def _terms(query: str, prefer: Iterable[str] = ()) -> _Terms:
    """Analyse a query: significant tokens, mandatory proper nouns, head noun, and fallback queries.

    Commons search ANDs every word, so "Gwangjang Market bindaetteok" or "bowl of jjajangmyeon" can
    return nothing. Fallbacks, in order: drop generic framing words ("jjajangmyeon"), keep just the
    proper nouns when they form a multi-word name ("Gwangjang Market"; a lone "Showa", "Seoul" or
    "Brazilian" matches far too much), keep the last two words, i.e. the head noun phrase
    ("kitchen timer" for "retro kitchen timer"). Every fallback must reach MIN_RELEVANCE against
    the FULL query, and candidates still need the head noun (see ``_relevance``).
    """
    raw_words = re.findall(r"[^\W_]+", query)
    folded = [_fold(w) for w in raw_words]
    pairs = [(raw, f) for raw, f in zip(raw_words, folded) if f and f not in _STOPWORDS]
    specific = [(raw, f) for raw, f in pairs if f not in _GENERIC] or pairs
    tokens = tuple(dict.fromkeys(f for _, f in specific))
    # Capitalised words are proper nouns ("Han River park" needs "han" AND "river"). In an
    # all-Title-Case query capitals carry no signal, so only the leading subject is required.
    capitalised = {f for raw, f in specific if raw[:1].isupper()}
    if len(tokens) == 1 or (len(tokens) > 1 and len(capitalised) == len(tokens)):
        required, proper = frozenset(tokens[:1]), frozenset()
    else:
        required = proper = frozenset(capitalised)

    core = [raw for raw, f in specific if f in required]
    if len(core) < 2:
        core = []  # one proper noun alone ("Showa" -> Emperor Showa, "Seoul" -> anything) is too loose
    if len(specific) >= 3:
        head = " ".join(raw for raw, _ in specific[-2:])
    elif len(specific) == 2:
        head = max((raw for raw, _ in specific), key=len)  # longer word ~ more specific subject
    else:
        head = ""
    options = [
        (" ".join(raw for raw, _ in specific), MIN_RELEVANCE),
        (" ".join(core), MIN_RELEVANCE),
        (head, MIN_RELEVANCE),
    ]
    seen = {_fold(query)}
    fallbacks = []
    for text, min_rel in options:
        if text and _fold(text) not in seen:
            seen.add(_fold(text))
            fallbacks.append((text, min_rel))
    prefer = tuple(prefer)
    prefer_words = frozenset(w for p in prefer for w in _fold(p).split() if w.isascii() and len(w) >= 3)
    return _Terms(
        tokens, required, tokens[-1] if tokens else "", tuple(fallbacks), proper, prefer_words,
        frozenset(_scripts(" ".join(prefer))),
    )


def _scripts(text: str) -> set[str]:
    """Non-Latin scripts of the letters in ``text`` ("한강" -> {"HANGUL"}, "チーム" -> {"KATAKANA"})."""
    return {
        unicodedata.name(ch, "?").split(" ")[0] for ch in text if ch.isalpha() and not ch.isascii()
    } - {"LATIN", "?"}


def _token_matches(token: str, words: list[str], exact_plural: bool = False) -> bool:
    if len(token) <= 3:
        return token in words
    stem = token[:-1] if token.endswith("s") and len(token) > 4 and not exact_plural else token
    for word in words:
        if word.startswith(stem):
            return True
        # Romanization drift: "jjajangmyeon" vs "jajangmyeon", "tteokbokki" vs "ddeokbokki".
        if len(token) >= 6 and abs(len(word) - len(token)) <= 2:
            if difflib.SequenceMatcher(None, token, word).ratio() >= 0.85:
                return True
    return False


def _relevance(terms: _Terms, title: str, detail: str = "") -> float:
    """Share of significant tokens found in ``title`` + ``detail``.

    Required proper nouns must appear in ``title`` itself (title/tags are reliable; free-text
    descriptions sometimes name unrelated places), else the result is 0. The head noun (last
    word) must appear too, unless the query has a multi-word proper name (already required in the
    title) or at least 3/4 of its words match (a trailing adjective, as in "pao de queijo
    quentinho"): "Han River at dusk" may illustrate "Han River park", but "Triangle file" never
    illustrates "triangle kimbap" and "Emperor Showa" never illustrates "Showa kissaten".

    The query's FIRST word must match as well: the prompt asks for the specific subject first
    ("leather school satchel", "chin chin snack box"), and half-matches on the trailing category
    words showed "Satchel Paige" and an elephant-shaped snack box in live games.
    """
    if not terms.tokens:
        return 0.0
    title_words = _fold(title).split()
    if not all(_token_matches(t, title_words, exact_plural=t in terms.proper) for t in terms.required):
        return 0.0
    words = title_words + _fold(detail).split()
    if len(terms.tokens) >= 2 and not _token_matches(terms.tokens[0], words, exact_plural=terms.tokens[0] in terms.proper):
        return 0.0
    share = sum(1 for t in terms.tokens if _token_matches(t, words)) / len(terms.tokens)
    if share < HEAD_OPTIONAL_SHARE and len(terms.required) < 2 and not _token_matches(terms.head, words):
        return 0.0
    return share


# Demonym endings, as in quiz._DEMONYM_ENDINGS: "korea" also prefers "korean", "japan" "japanese".
_PREFER_ENDINGS = ("n", "ese", "ish", "i", "ic", "ine")


def _mentions_country(terms: _Terms, title: str, detail: str = "") -> bool:
    """Does the candidate name the player's country (``terms.prefer``) in its title or description,
    or use the script of its native names (Commons describes many Seoul photos only in Hangul)?"""
    if terms.prefer_scripts and terms.prefer_scripts & _scripts(f"{title} {detail}"):
        return True
    if not terms.prefer:
        return False
    for word in f"{_fold(title)} {_fold(detail)}".split():
        for p in terms.prefer:
            if word == p or (len(p) >= 4 and word.startswith(p[:4]) and word.endswith(_PREFER_ENDINGS)):
                return True
    return False


def _strip_html(value: Any, limit: int = 300) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(html.unescape(text).split())[:limit]


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _ext(name: str) -> str:
    tail = name.rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[-1].lower() if "." in tail else ""


def _file_key(url: str) -> str:
    """Identity of a photo: Wikimedia files by file name (one file has many thumbnail URLs:
    ".../thumb/a/ab/Name.jpg/960px-Name.jpg", ".../a/ab/Name.jpg"), anything else by URL."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.endswith("wikimedia.org") or host.endswith("wikipedia.org"):
        segments = [s for s in parts.path.split("/") if s]
        if "thumb" in segments:
            i = segments.index("thumb")
            if len(segments) > i + 3:
                return "file:" + unquote(segments[i + 3]).replace(" ", "_").casefold()
        if segments:
            return "file:" + unquote(segments[-1]).replace(" ", "_").casefold()
    return url


# --------------------------------------------------------------------------- what a card wants


@dataclass(frozen=True)
class PhotoSpec:
    """What one card should show (built by quiz.py from the question model's option)."""

    label: str  # the card's English label ("Bindaetteok at Gwangjang Market")
    query: str  # image_query: 2-5 English words naming the photographable subject
    sublabel: str | None = None  # native-script name ("광장시장 빈대떡"); also a parity-tier query
    wiki_lang: str | None = None  # Wikipedia language code of ``wiki_title`` ("ko")
    wiki_title: str | None = None  # article whose lead photo shows the thing ("빈대떡")
    prefer: tuple[str, ...] = ()  # country names / native script: Commons photos naming them rank first

    def describe(self) -> str:
        sub = f" ({self.sublabel})" if self.sublabel else ""
        wiki = f"; Wikipedia article {self.wiki_lang}:{self.wiki_title}" if self.wiki_title else ""
        return f'"{self.label}"{sub}; photo subject: {self.query}{wiki}'


def _wiki_ref(spec: PhotoSpec) -> tuple[str, str] | None:
    lang = (spec.wiki_lang or "").strip().lower()
    title = " ".join(str(spec.wiki_title or "").replace("|", " ").replace("_", " ").split())[:200]
    if not title or not _LANG_RE.match(lang):
        return None
    return lang, title


# --------------------------------------------------------------------------- candidates


@dataclass(frozen=True)
class _Candidate:
    url: str
    width: int
    height: int
    title: str  # strong relevance signal: Commons file title, or the Wikipedia article title
    detail: str  # weak relevance signal: free-text description
    creator: str
    license: str
    source_url: str | None
    source: str  # "Wikipedia" | "Wikimedia Commons"
    rank: int  # position in the provider's ranking (0 = best)
    tier: int = 2  # 1 = Wikipedia lead image, 2 = Commons search, 3 = parity tier
    categories: str = ""
    file: str = ""  # file name ("Bindae-tteok.jpg")
    article: str = ""  # "ko:빈대떡 (녹두에 ... 부침개)" for lead images

    @property
    def key(self) -> str:
        return _file_key(self.url)

    def for_judge(self) -> str:
        parts = []
        if self.article:
            parts.append(f"lead photo of Wikipedia article {self.article}")
            parts.append(f'file "{self.file}"')
        else:
            parts.append(f'Commons photo "{self.title[:140]}"')
        if self.detail:
            parts.append(f"description: {self.detail[:200]}")
        if self.categories:
            parts.append(f"categories: {self.categories[:220]}")
        return "; ".join(parts)


def _shape_ok(width: int, height: int) -> bool:
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return False
    return min(width / height, height / width) >= MIN_SQUARENESS


def _score(c: _Candidate, terms: _Terms, min_relevance: float = MIN_RELEVANCE) -> float | None:
    """Commons ranking. Higher is better; None = reject."""
    if c.height <= 0 or not _shape_ok(c.width, c.height):
        return None
    if _is_sensitive(c.title, f"{c.detail} {c.categories}"):
        return None
    relevance = _relevance(terms, c.title, c.detail)
    if relevance < min_relevance:
        return None
    squareness = min(c.width / c.height, c.height / c.width)
    bonus = COUNTRY_BONUS if _mentions_country(terms, c.title, c.detail) else 0.0
    return relevance * 3 + squareness * 2 + min(c.width, 1200) / 1200 * 0.5 - c.rank * 0.08 + bonus


def _ranked(candidates: Iterable[_Candidate], terms: _Terms, min_relevance: float = MIN_RELEVANCE) -> list[_Candidate]:
    """Acceptable Commons candidates, best first, each file once."""
    scored = sorted(
        ((s, c) for c in candidates if (s := _score(c, terms, min_relevance)) is not None),
        key=lambda sc: sc[0],
        reverse=True,
    )
    seen: set[str] = set()
    return [c for _, c in scored if not (c.key in seen or seen.add(c.key))]


def _best(candidates: list[_Candidate], terms: _Terms, min_relevance: float = MIN_RELEVANCE) -> _Candidate | None:
    ranked = _ranked(candidates, terms, min_relevance)
    return ranked[0] if ranked else None


def _credit(c: _Candidate) -> str:
    parts = [p for p in (c.creator, c.license) if p]
    text = " / ".join(parts) if parts else "Unknown"
    return f"{text[:90]} via {c.source}"


def _option_image(c: _Candidate) -> OptionImage:
    return OptionImage(url=c.url, credit=_credit(c), source_url=c.source_url)


# Maintenance categories say nothing about what a photo shows ("Taken with Nikon D40", "Images from
# Wiki Loves Africa 2017", "Files with coordinates missing SDC location of creation").
_JUNK_CATEGORY_RE = re.compile(
    r"^(?:files?\b|taken with|images? (?:from|by|with)|photos? (?:from|by)|photographs taken|pages\b|media\b|"
    r"creative commons|cc[- ]|pd[- ]|gfdl|licen|self[- ]published|uploaded|featured|quality images|valued images|"
    r"flickr|wiki loves|unidentified|supported by|uploads? by|.*\bmissing sdc\b|.*\bphotographs taken on\b|"
    r".*\bwith known ids\b|.*\bto check\b)",
    re.IGNORECASE,
)


def _categories(meta: dict[str, Any]) -> str:
    raw = _strip_html((meta.get("Categories") or {}).get("value"), 1500)
    cats = [c.strip() for c in raw.split("|") if c.strip()]
    return ", ".join(c for c in cats if not _JUNK_CATEGORY_RE.match(c))[:240]


# --------------------------------------------------------------------------- HTTP


class _Budget:
    """Token bucket for our Wikimedia requests. Wikimedia allows an identified bot (a User-Agent
    with a contact URL) about 200 requests a minute across all its sites (2026 rate limits) and
    answers 429 + a pause beyond that; a round with its speculative prefetch makes ~8-12 requests.
    A request that would have to queue longer than MAX_QUEUE_S is skipped instead."""

    def __init__(self, per_minute: float, burst: float) -> None:
        self.rate = per_minute / 60.0
        self.burst = burst
        self.tokens = burst
        self.stamp = time.monotonic()
        self.paused_until = 0.0  # after a 429: no requests until then

    def reserve(self) -> float | None:
        """Take a token: seconds to wait before using it, or None (paused, or the queue is too long)."""
        now = time.monotonic()
        if now < self.paused_until:
            return None
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        wait = 0.0 if self.tokens >= 1 else (1 - self.tokens) / self.rate
        if wait > MAX_QUEUE_S:
            return None
        self.tokens -= 1
        return wait

    def pause(self, seconds: float) -> None:
        self.paused_until = max(self.paused_until, time.monotonic() + seconds)


_budget = _Budget(REQUESTS_PER_MIN, REQUEST_BURST)


def _note_rate_limit(host: str, resp: httpx.Response) -> None:
    """Wikimedia's limits are per client across all its sites: pause every request, not just this host."""
    retry_after = _int(resp.headers.get("retry-after"))
    seconds = float(retry_after) if 0 < retry_after <= 600 else BACKOFF_S
    _budget.pause(seconds)
    log.warning("image API %s is rate limiting us (429); pausing photo lookups for %.0fs", host, seconds)


async def _get_json(url: str, params: dict[str, Any]) -> Any:
    """GET a Wikimedia API URL -> parsed JSON, or None (timeout, HTTP error, junk, over our request
    budget, or paused after a 429). Never raises."""
    host = urlsplit(url).hostname or ""
    wait = _budget.reserve()
    if wait is None:
        log.info("image API %s skipped: request budget exhausted", host)
        return None
    if wait:
        await asyncio.sleep(wait)
    async with _semaphore():
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                resp = await _client().get(url, params=params)
            if resp.status_code == 429:
                _note_rate_limit(host, resp)
                return None
            if resp.status_code != 200:
                log.info("image API %s returned HTTP %s", host, resp.status_code)
                return None
            return resp.json()
        except (TimeoutError, httpx.HTTPError, ValueError) as err:
            log.info("image API %s failed: %s", host, type(err).__name__)
        except asyncio.CancelledError:
            raise
        except Exception:  # never let an image problem break a question
            log.exception("image API %s crashed", host)
    return None


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=httpx.Timeout(REQUEST_TIMEOUT_S),
        follow_redirects=True,
        transport=_transport,
    )


# One keep-alive client (and request semaphore) per event loop: a quiz round makes ~10 requests,
# and fresh TLS handshakes for each would eat the budget. Recreated if the loop changes (tests).
_shared: tuple[asyncio.AbstractEventLoop, httpx.AsyncClient, asyncio.Semaphore] | None = None


def _loop_state() -> tuple[asyncio.AbstractEventLoop, httpx.AsyncClient, asyncio.Semaphore]:
    global _shared
    loop = asyncio.get_running_loop()
    if _shared is None or _shared[0] is not loop or _shared[1].is_closed:
        _shared = (loop, _make_client(), asyncio.Semaphore(MAX_CONCURRENT_REQUESTS))
    return _shared


def _client() -> httpx.AsyncClient:
    return _loop_state()[1]


def _semaphore() -> asyncio.Semaphore:
    return _loop_state()[2]


async def aclose() -> None:
    """Close the shared HTTP client (optional; call on app shutdown)."""
    global _shared
    if _shared is not None and _shared[0] is asyncio.get_running_loop():
        await _shared[1].aclose()
    _shared = None


# --------------------------------------------------------------------------- caches


class _Memo:
    """Async TTL + LRU memo of candidate lists. Concurrent callers share one in-flight task, which
    is never cancelled by a caller giving up (a cancelled prefetch still fills the cache); empty
    results expire after MISS_TTL_S, others after HIT_TTL_S."""

    def __init__(self) -> None:
        self._data: OrderedDict[str, tuple[float, tuple[_Candidate, ...]]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[tuple[_Candidate, ...]]] = {}

    def task(self, key: str, make: Callable[[], Awaitable[list[_Candidate]]]) -> asyncio.Future[tuple[_Candidate, ...]]:
        loop = asyncio.get_running_loop()
        entry = self._data.get(key)
        if entry is not None and time.monotonic() <= entry[0]:
            self._data.move_to_end(key)
            done: asyncio.Future[tuple[_Candidate, ...]] = loop.create_future()
            done.set_result(entry[1])
            return done
        task = self._inflight.get(key)
        if task is None or task.get_loop() is not loop:
            task = loop.create_task(self._run(key, make), name=f"image-lookup:{key[:40]}")
            self._inflight[key] = task
            task.add_done_callback(lambda t, k=key: self._inflight.pop(k, None) if self._inflight.get(k) is t else None)
        return task

    async def _run(self, key: str, make: Callable[[], Awaitable[list[_Candidate]]]) -> tuple[_Candidate, ...]:
        try:
            value = tuple(await make())
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - the fetchers already guard every request
            log.exception("image lookup crashed")
            value = ()
        self._data[key] = (time.monotonic() + (HIT_TTL_S if value else MISS_TTL_S), value)
        self._data.move_to_end(key)
        while len(self._data) > CACHE_SIZE:
            self._data.popitem(last=False)
        return value

    def clear(self) -> None:
        self._data.clear()
        self._inflight.clear()


_commons_results = _Memo()  # folded query (+ preference) -> ranked Commons candidates
_wiki_searches = _Memo()  # "lang:query" -> Wikipedia search lead images (parity tier)
_leads: OrderedDict[str, tuple[float, tuple[_Candidate, ...]]] = OrderedDict()  # "lang:title" -> lead image
_verdicts: OrderedDict[tuple[str, str], str] = OrderedDict()  # (card, photo) -> judge verdict
_running: set[asyncio.Future[Any]] = set()  # lead lookups that outlived their round (they fill the cache)


def clear_cache() -> None:
    """Forget cached results, in-flight lookups, judge verdicts, backoffs and the shared client (tests)."""
    global _shared
    for memo in (_commons_results, _wiki_searches):
        memo.clear()
    _leads.clear()
    _verdicts.clear()
    _budget.__init__(REQUESTS_PER_MIN, REQUEST_BURST)
    _shared = None


# --------------------------------------------------------------------------- tier 1: Wikipedia


def _wiki_api(lang: str) -> str:
    return f"https://{lang}.wikipedia.org/w/api.php"


_PAGEIMAGE_PARAMS = {
    "prop": "pageimages|pageprops|description",
    "piprop": "thumbnail|name|original",
    "pithumbsize": THUMB_WIDTH,
    "pilicense": "any",  # include fair-use covers and logos so pop-culture icons get a picture
    "ppprop": "disambiguation",
    "format": "json",
    "formatversion": 2,
}


def _pages(data: Any) -> list[dict[str, Any]]:
    query = data.get("query") if isinstance(data, dict) else None
    pages = (query or {}).get("pages") or []
    if isinstance(pages, dict):  # formatversion=1 shape
        pages = list(pages.values())
    return sorted((p for p in pages if isinstance(p, dict)), key=lambda p: _int(p.get("index")) or 0)


def _lead_candidate(page: dict[str, Any], lang: str, rank: int, card_kind_ok: bool) -> _Candidate | None:
    """The lead image of one article as a candidate, or None (missing page, disambiguation page,
    no free image, too small, a panorama, or a logo / map / flag / diagram / SVG for a card that
    isn't one)."""
    if page.get("missing") or page.get("invalid") or "disambiguation" in (page.get("pageprops") or {}):
        return None
    name = str(page.get("pageimage") or "")
    thumb = page.get("thumbnail") or {}
    original = page.get("original") or {}
    url = str(thumb.get("source") or "")
    if not name or not url.startswith("https://"):
        return None
    ext = _ext(name)
    non_photo = ext == "svg" or bool(_NON_PHOTO_RE.search(_fold(name.rsplit(".", 1)[0])))
    if (ext not in _ALLOWED_EXT and ext != "svg") or (non_photo and not card_kind_ok):
        return None
    width = _int(original.get("width")) or _int(thumb.get("width"))
    height = _int(original.get("height")) or _int(thumb.get("height"))
    if ext != "svg" and not _shape_ok(width, height):
        return None
    on_commons = "/wikipedia/commons/" in url
    file_page = (
        f"https://commons.wikimedia.org/wiki/File:{quote(name)}" if on_commons
        else f"https://{lang}.wikipedia.org/wiki/File:{quote(name)}"
    )
    title = str(page.get("title") or "")
    description = _strip_html(page.get("description"), 160)
    return _Candidate(
        url=url, width=width, height=height, title=title, detail="", creator="", license="",
        source_url=file_page, source="Wikipedia", rank=rank, tier=1, file=name,
        article=f'{lang}:{title}' + (f" ({description})" if description else ""),
    )


async def _with_file_info(cands: list[_Candidate]) -> list[_Candidate]:
    """Add each lead image's own description, categories, author and license (for the judge and
    the credit) from Commons. Files only on a local wiki keep their article context."""
    names = [c.file for c in cands if c.source_url and c.source_url.startswith("https://commons.")]
    if not names:
        return cands
    data = await _get_json(COMMONS_API, {
        "action": "query", "titles": "|".join(f"File:{n}" for n in names[:20]), "prop": "imageinfo",
        "iiprop": "extmetadata", "iiextmetadatafilter": "Artist|LicenseShortName|ImageDescription|Categories",
        "format": "json", "formatversion": 2,
    })
    info: dict[str, dict[str, Any]] = {}
    for page in _pages(data):
        meta = ((page.get("imageinfo") or [{}])[0] or {}).get("extmetadata") or {}
        info[_fold(str(page.get("title") or "").removeprefix("File:"))] = meta
    out = []
    for c in cands:
        meta = info.get(_fold(c.file))
        if meta is None:
            out.append(c)
            continue
        out.append(replace(
            c,
            detail=_strip_html((meta.get("ImageDescription") or {}).get("value"), 240),
            categories=_categories(meta),
            creator=_strip_html((meta.get("Artist") or {}).get("value"), 60),
            license=_strip_html((meta.get("LicenseShortName") or {}).get("value"), 40),
        ))
    return out


def _resolve(data: Any, titles: list[str]) -> dict[str, dict[str, Any]]:
    """Map each requested title to its page in a ``titles=A|B`` answer (following the API's
    ``normalized`` and ``redirects`` hops)."""
    query = data.get("query") if isinstance(data, dict) else None
    hops: dict[str, str] = {}
    for key in ("normalized", "redirects"):
        for hop in (query or {}).get(key) or []:
            if isinstance(hop, dict) and hop.get("from") and hop.get("to"):
                hops[str(hop["from"])] = str(hop["to"])
    by_title = {str(p.get("title")): p for p in _pages(data)}
    out = {}
    for title in titles:
        current, seen = title, set()
        while current in hops and current not in seen:
            seen.add(current)
            current = hops[current]
        if current in by_title:
            out[title] = by_title[current]
    return out


async def _title_pages(lang: str, titles: list[str], langlinks: bool) -> dict[str, dict[str, Any]]:
    """One request for several articles of one wiki: their lead images (and English counterparts)."""
    params: dict[str, Any] = {"action": "query", "titles": "|".join(titles), "redirects": 1, **_PAGEIMAGE_PARAMS}
    if langlinks:
        params |= {"prop": f"{_PAGEIMAGE_PARAMS['prop']}|langlinks", "lllang": "en"}
    return _resolve(await _get_json(_wiki_api(lang), params), titles)


def _lead_key(lang: str, title: str, kind_ok: bool) -> str:
    return f"{lang}:{_fold(title)}:{int(kind_ok)}"


async def _lead_images(refs: list[tuple[str, str, bool] | None]) -> list[list[_Candidate]]:
    """Tier 1 for every card of a round with few requests: one per wiki language (all titles
    together, redirects followed); a title that doesn't exist (or is a disambiguation page) falls
    back to that wiki's search, an article without a usable free lead image to its English
    counterpart; then ONE Commons request adds every file's description, categories, author and
    license. Results are cached per article."""
    out: list[list[_Candidate]] = [[] for _ in refs]
    todo: dict[str, list[int]] = {}
    now = time.monotonic()
    for i, ref in enumerate(refs):
        if ref is None:
            continue
        entry = _leads.get(_lead_key(*ref))
        if entry is not None and now <= entry[0]:
            out[i] = list(entry[1])
        else:
            todo.setdefault(ref[0], []).append(i)
    if not todo:
        return out

    async def english_lead(i: int, title: str, kind_ok: bool) -> None:
        page = (await _title_pages("en", [title], langlinks=False)).get(title)
        if page and (found := _lead_candidate(page, "en", 0, kind_ok)):
            out[i] = [found]

    async def searched(i: int, lang: str, title: str, kind_ok: bool) -> None:
        out[i] = await _wiki_search(lang, title, kind_ok)

    async def first_pass(lang: str, idx: list[int]) -> None:
        titles = list(dict.fromkeys(refs[i][1] for i in idx))
        pages = await _title_pages(lang, titles, langlinks=lang != "en")
        later = []
        for i in idx:
            _, title, kind_ok = refs[i]
            page = pages.get(title)
            if page and (found := _lead_candidate(page, lang, 0, kind_ok)):
                out[i] = [found]
            elif page and not page.get("missing") and "disambiguation" not in (page.get("pageprops") or {}):
                # A real article without a usable free lead image: try its English counterpart.
                links = page.get("langlinks") or []
                english = next((str(ll.get("title")) for ll in links if ll.get("lang") == "en" and ll.get("title")), None)
                if english:
                    later.append(english_lead(i, english, kind_ok))
            elif pages:  # the wiki answered, but this title is missing or a disambiguation page
                later.append(searched(i, lang, title, kind_ok))
        await asyncio.gather(*later)

    await asyncio.gather(*(first_pass(lang, idx) for lang, idx in todo.items()))
    fresh = [i for idx in todo.values() for i in idx]
    detailed = {c.key: c for c in await _with_file_info([c for i in fresh for c in out[i]])}
    for i in fresh:
        out[i] = [detailed.get(c.key, c) for c in out[i]]
        key = _lead_key(*refs[i])
        _leads[key] = (time.monotonic() + (HIT_TTL_S if out[i] else MISS_TTL_S), tuple(out[i]))
        _leads.move_to_end(key)
    while len(_leads) > CACHE_SIZE:
        _leads.popitem(last=False)
    return out


async def _wiki_search(lang: str, query: str, card_kind_ok: bool, tier: int = 1) -> list[_Candidate]:
    """Lead images of the top Wikipedia search hits for ``query`` (fuzzy titles, other spellings)."""
    data = await _get_json(_wiki_api(lang), {
        "action": "query", "generator": "search", "gsrsearch": query, "gsrlimit": 4, "gsrnamespace": 0,
        **_PAGEIMAGE_PARAMS,
    })
    out = []
    for rank, page in enumerate(_pages(data)):
        if (c := _lead_candidate(page, lang, rank, card_kind_ok)) is not None:
            out.append(replace(c, tier=tier))
    return out[:SEARCH_PER_CARD]


# --------------------------------------------------------------------------- tier 2: Commons search


def _parse_wikimedia(data: Any) -> list[_Candidate]:
    out: list[_Candidate] = []
    for rank, page in enumerate(_pages(data)):
        info = (page.get("imageinfo") or [{}])[0]
        if str(info.get("mime") or "").lower() not in _ALLOWED_MIME:
            continue
        url = str(info.get("thumburl") or info.get("url") or "")
        if not url.startswith("https://"):
            continue
        meta = info.get("extmetadata") or {}
        name = str(page.get("title") or "").removeprefix("File:")
        title = name.rsplit(".", 1)[0] if "." in name else name
        out.append(
            _Candidate(
                url=url,
                width=_int(info.get("width")),
                height=_int(info.get("height")),
                title=title,
                detail=_strip_html((meta.get("ImageDescription") or {}).get("value")),
                creator=_strip_html((meta.get("Artist") or {}).get("value"), 60),
                license=_strip_html((meta.get("LicenseShortName") or {}).get("value"), 40),
                source_url=info.get("descriptionurl") or None,
                source="Wikimedia Commons",
                rank=rank,
                categories=_categories(meta),
                file=name,
            )
        )
    return out


async def _wikimedia(query: str) -> list[_Candidate]:
    data = await _get_json(COMMONS_API, {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap",
        "gsrnamespace": 6,
        "gsrlimit": 10,
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiextmetadatafilter": "Artist|LicenseShortName|ImageDescription|Categories",
        "iiurlwidth": THUMB_WIDTH,
        "format": "json",
        "formatversion": 2,
    })
    return _parse_wikimedia(data) if data is not None else []


async def _commons(query: str, terms: _Terms) -> list[_Candidate]:
    """Tier 2: relevance-filtered Commons photos for the full photo query, best first."""
    return _ranked(await _wikimedia(query), terms)[:COMMONS_PER_CARD]


async def _commons_narrower(terms: _Terms) -> list[_Candidate]:
    """Parity tier: Commons with the narrower fallback queries (Commons ANDs every word of the full
    query), in order; the first that matches wins."""
    for fallback, min_relevance in terms.fallbacks:
        if ranked := _ranked(await _wikimedia(fallback), terms, min_relevance):
            return [replace(c, tier=3) for c in ranked[:COMMONS_PER_CARD]]
    return []


async def _commons_native(name: str) -> list[_Candidate]:
    """Parity tier: Commons files titled or described with the card's native-script name (no
    relevance filter: the judge decides)."""
    out = []
    for c in await _wikimedia(name):
        if _shape_ok(c.width, c.height) and not _is_sensitive(c.title, f"{c.detail} {c.categories}"):
            out.append(replace(c, tier=3))
    return out[:COMMONS_PER_CARD]


async def _searched_leads(lang: str, query: str, kind_ok: bool, tier: int) -> list[_Candidate]:
    """Lead images of Wikipedia search hits, with their file details (for the judge and the credit)."""
    return await _with_file_info(await _wiki_search(lang, query, kind_ok, tier=tier))


# --------------------------------------------------------------------------- per-card candidate sets


def _card_kind_ok(spec: PhotoSpec) -> bool:
    """May this card show a logo / flag / map / diagram (because that is what the card is)?"""
    return bool(_NON_PHOTO_CARD_RE.search(_fold(f"{spec.label} {spec.query}")))


def _commons_key(query: str, prefer: tuple[str, ...]) -> str:
    return f"{_fold(query)}|{'|'.join(prefer)}"


def _commons_first(spec: PhotoSpec) -> asyncio.Future[tuple[_Candidate, ...]] | None:
    """Tier 2: Commons photos for the card's photo query (cached / shared future)."""
    query = _normalize_query(spec.query)
    terms = _terms(query, spec.prefer)
    if not terms.tokens:
        return None
    return _commons_results.task(_commons_key(query, spec.prefer), lambda: _commons(query, terms))


def _next_tiers(spec: PhotoSpec) -> list[asyncio.Future[tuple[_Candidate, ...]]]:
    """The parity tier for a card without a good photo: narrower Commons queries, an English
    Wikipedia search for its photo query, and its native-script name searched on its Wikipedia
    and on Commons (local photos are often titled only in the local script)."""
    futures = []
    kind_ok = _card_kind_ok(spec)
    query = _normalize_query(spec.query)
    terms = _terms(query, spec.prefer)
    if terms.fallbacks:
        futures.append(_commons_results.task(f"narrow|{_commons_key(query, spec.prefer)}", lambda: _commons_narrower(terms)))
    if terms.tokens:
        futures.append(_wiki_searches.task(f"en:{_fold(query)}:{kind_ok}", lambda: _searched_leads("en", query, kind_ok, 3)))
    sublabel = " ".join(str(spec.sublabel or "").split())[:80]
    if sublabel and _fold(sublabel) != _fold(query):
        ref = _wiki_ref(spec)
        if ref and ref[0] != "en":
            lang = ref[0]
            futures.append(_wiki_searches.task(
                f"{lang}:{_fold(sublabel)}:{kind_ok}", lambda: _searched_leads(lang, sublabel, kind_ok, 3)
            ))
        if _scripts(sublabel):
            futures.append(_commons_results.task(f"native|{sublabel}", lambda: _commons_native(sublabel)))
    return futures


def _result(future: asyncio.Future[Any] | None, default: Any) -> Any:
    if future is None or not future.done() or future.cancelled() or future.exception() is not None:
        return default
    return future.result()


async def _first_candidates(a: PhotoSpec, b: PhotoSpec) -> list[list[_Candidate]]:
    """Both cards' tier 1 + tier 2 candidates (in that order) that arrived within GATHER_S. Lookups
    still running then keep going in the background and fill the caches."""
    refs = [(*ref, _card_kind_ok(spec)) if (ref := _wiki_ref(spec)) else None for spec in (a, b)]
    leads = asyncio.ensure_future(_lead_images(refs))
    commons = [_commons_first(a), _commons_first(b)]
    pending = {f for f in (leads, *commons) if f is not None and not f.done()}
    if pending:
        await asyncio.wait(pending, timeout=GATHER_S)
    if not leads.done():
        _running.add(leads)
        leads.add_done_callback(_running.discard)
    lead_lists = _result(leads, [[], []])
    return [list(lead) + list(_result(future, ())) for lead, future in zip(lead_lists, commons)]


async def _collect(groups: list[list[asyncio.Future[tuple[_Candidate, ...]]]], seconds: float) -> list[list[_Candidate]]:
    """Each card's candidates from its futures (in tier order) that finished within ``seconds``.
    Unfinished lookups keep running and land in the cache."""
    pending = {f for group in groups for f in group if not f.done()}
    if pending:
        await asyncio.wait(pending, timeout=seconds)
    out = []
    for group in groups:
        merged: list[_Candidate] = []
        for future in group:
            if future.done() and not future.cancelled() and future.exception() is None:
                merged.extend(future.result())
        out.append(merged)
    return out


def _usable(cands: list[_Candidate], skip: set[str]) -> list[_Candidate]:
    """Drop photos already shown (``skip`` holds file keys), repeats, and blocklisted text."""
    seen = set(skip)
    out = []
    for c in cands:
        if c.key in seen or _is_sensitive(f"{c.title} {c.article}", f"{c.detail} {c.categories}"):
            continue
        seen.add(c.key)
        out.append(c)
    return out


# --------------------------------------------------------------------------- the judge

JUDGE_SYSTEM = """\
You screen candidate photos for the answer cards of a light, friendly party quiz (a balance game). Each card \
names one concrete thing (a food, place, object or activity) and gets one photo. For every candidate you get \
where it comes from (the lead photo of a Wikipedia article, or a Wikimedia Commons file), its file title, \
description and categories, in any language: read them all (Korean, Japanese, Portuguese, Yoruba, Spanish...).

Give each candidate exactly one verdict:
- "unsafe": the photo is about, or likely shows: war, weapons, the military or police; atrocities, slavery, \
colonial violence; disasters, floods, fires or accidents; memorials, monuments to the dead, graves, funerals or \
death; politicians, royalty or religious figures (including statues of them); religious rituals, worship, \
offerings, sacred art or places of worship (temples, shrines, churches, mosques); a specific person as the main \
subject (a portrait, a named person or job title such as "CEO", one described individual such as "woman weaving", \
"young lady displaying", "man selling fish", "child eating", or categories like "People of Lagos", "Portraits", \
"Couples"); nudity or sexual content; extremist or hate symbols; drugs. Crowds, performers on a stage and people \
as small background figures are fine. When unsure, "unsafe".
- "yes": safe, and clearly shows the card's thing itself (that food, object, place, activity or scene).
- "partial": safe and related, but not really the card's thing (a different variety, the wider place, the \
same category, an advertisement for it).
- "no": safe but unrelated or a different thing; also logos, maps, flags, coats of arms, diagrams, charts, \
posters, documents and screenshots unless the card's thing IS one.
Reply with ONE JSON object mapping every candidate id to its verdict, e.g. {"a1": "yes", "a2": "no", "b1": "unsafe"}."""

_VERDICT_WORDS = {"yes": "yes", "partial": "partial", "no": "no", "unsafe": "unsafe"}


def _card_signature(spec: PhotoSpec) -> str:
    return _fold(f"{spec.label}|{spec.sublabel or ''}|{spec.query}")


def _normalize_verdict(value: Any) -> str:
    text = _fold(str(value or ""))
    for word in text.split():
        if word in _VERDICT_WORDS:
            return _VERDICT_WORDS[word]
    return "no"


async def _judge(cards: list[tuple[str, PhotoSpec, list[_Candidate]]], context: str) -> dict[tuple[str, str], str] | None:
    """Verdicts {(card signature, file key): "yes" | "partial" | "no" | "unsafe"} for every candidate,
    from ONE LLM call (cached verdicts are reused). None if the judge failed."""
    known: dict[tuple[str, str], str] = {}
    lines: list[str] = []
    ids: dict[str, tuple[str, str]] = {}
    for side, spec, cands in cards:
        sig = _card_signature(spec)
        todo = []
        for c in cands:
            if (sig, c.key) in _verdicts:
                known[(sig, c.key)] = _verdicts[(sig, c.key)]
            else:
                todo.append(c)
        if not todo:
            continue
        lines.append(f"Card {side.upper()}: {spec.describe()}")
        for i, c in enumerate(todo, 1):
            cid = f"{side}{i}"
            ids[cid] = (sig, c.key)
            lines.append(f"  {cid}: {c.for_judge()}")
    if not ids:
        return known
    user = f"{context}\n\n" + "\n".join(lines) + "\n\nReturn the JSON verdicts now."
    started = time.perf_counter()
    try:
        async with asyncio.timeout(JUDGE_TIMEOUT_S):
            raw = await llm.chat_json(
                JUDGE_MODEL, JUDGE_SYSTEM, user, dict[str, Any], max_tokens=JUDGE_MAX_TOKENS,
                temperature=0, reasoning_effort="none", hedge_after=JUDGE_HEDGE_S,
            )
    except (LLMError, TimeoutError) as err:
        log.warning("photo judge failed (%s) after %.1fs", type(err).__name__, time.perf_counter() - started)
        return None
    log.info("photo judge: %d candidate(s) in %.1fs", len(ids), time.perf_counter() - started)
    raw = raw if isinstance(raw, dict) else {}
    if len(raw) == 1 and isinstance(inner := next(iter(raw.values())), dict) and not set(raw) & set(ids):
        raw = inner  # {"verdicts": {"a1": "yes", ...}}
    for cid, key in ids.items():
        verdict = _normalize_verdict(raw.get(cid))  # a missing id counts as "no"
        known[key] = _verdicts[key] = verdict
        _verdicts.move_to_end(key)
    while len(_verdicts) > CACHE_SIZE * 8:
        _verdicts.popitem(last=False)
    return known


def _pick(spec: PhotoSpec, cands: list[_Candidate], verdicts: dict[tuple[str, str], str] | None,
          taken: set[str], verdict: str = "yes") -> _Candidate | None:
    """The card's first candidate (tier order) with this judge ``verdict`` that isn't taken. Without
    a judge (it failed), only Wikipedia lead images are trusted (the blocklist was applied)."""
    sig = _card_signature(spec)
    for c in cands:
        if c.key in taken:
            continue
        if verdicts is None:
            if c.tier == 1 and verdict == "yes":
                return c
        elif verdicts.get((sig, c.key)) == verdict:
            return c
    return None


def _choose(specs: dict[str, PhotoSpec], cands: dict[str, list[_Candidate]],
            verdicts: dict[tuple[str, str], str] | None, skip: set[str], partial: bool) -> dict[str, _Candidate | None]:
    """Each card's photo: its first "yes" (with ``partial``, else its first "partial"), never a
    skipped photo and never the other card's."""
    picks: dict[str, _Candidate | None] = {}
    taken = set(skip)
    for side in ("a", "b"):
        pick = _pick(specs[side], cands[side], verdicts, taken)
        if pick is None and partial:
            pick = _pick(specs[side], cands[side], verdicts, taken, "partial")
        picks[side] = pick
        if pick is not None:
            taken.add(pick.key)
    return picks


# --------------------------------------------------------------------------- public API


async def _next_candidates(sides: list[str], specs: dict[str, PhotoSpec], skip: set[str],
                           hurry: asyncio.Event | None) -> dict[str, list[_Candidate]]:
    """The given cards' next-tier candidates (not ``skip``ped), fetched concurrently."""
    window = HURRIED_GATHER_S if hurry is not None and hurry.is_set() else GATHER_S
    groups = await _collect([_next_tiers(specs[s]) for s in sides], window)
    return {s: _usable(group, skip)[:JUDGE_PER_CARD] for s, group in zip(sides, groups)}


async def find_pair(
    a: PhotoSpec,
    b: PhotoSpec,
    *,
    exclude: Iterable[str] = (),
    context: str = "",
    hurry: asyncio.Event | None = None,
) -> tuple[OptionImage | None, OptionImage | None]:
    """Photos for both cards of a round, or (None, None).

    ``exclude`` holds photo URLs already shown in this game (any thumbnail of the same file counts).
    ``context`` (country, question prompt) helps the judge. A card prefers a photo the judge called
    "yes"; a card with no candidates at all gets its next tier before the (single) judge call, and
    one whose candidates were all turned down gets it afterwards (one more judge call); failing
    that, it takes its best "partial" photo. Once ``hurry`` is set (a player is waiting for this
    round) the next tier gets a shorter window. Never raises (except ``CancelledError``); the
    caller bounds the total time.
    """
    started = time.perf_counter()
    skip = {_file_key(u) for u in exclude}
    specs = {"a": a, "b": b}
    groups = await _first_candidates(a, b)
    cands = {side: _usable(group, skip)[:JUDGE_PER_CARD] for side, group in zip(("a", "b"), groups)}
    extended = [s for s in ("a", "b") if not cands[s]]
    if extended:  # nothing at all for a card yet: its next tier joins the one judge call
        cands |= await _next_candidates(extended, specs, skip, hurry)
    gathered = time.perf_counter()
    verdicts: dict[tuple[str, str], str] | None = {}
    if any(cands.values()):
        verdicts = await _judge([(s, specs[s], cands[s]) for s in ("a", "b")], context)
    picks = _choose(specs, cands, verdicts, skip, partial=False)

    missing = [s for s in ("a", "b") if picks[s] is None and s not in extended]
    if missing and verdicts is not None:
        # Parity: before settling for a lesser photo (or none), try the card's next tier.
        tried = {c.key for group in cands.values() for c in group}
        more = await _next_candidates(missing, specs, skip | tried, hurry)
        if any(more.values()):
            extra = await _judge([(s, specs[s], more[s]) for s in missing], context)
            if extra is not None:
                verdicts = {**verdicts, **extra}
                for s in missing:
                    cands[s] = cands[s] + more[s]
    picks = _choose(specs, cands, verdicts, skip, partial=True)
    log.info(
        "photos in %.1fs (candidates %.1fs; A %s, B %s): %s | %s",
        time.perf_counter() - started, gathered - started, len(cands["a"]), len(cands["b"]),
        _describe_pick(picks["a"]), _describe_pick(picks["b"]),
    )
    if picks["a"] is None or picks["b"] is None:
        if picks["a"] or picks["b"]:
            log.info("quiz: only one card had a photo; using the gradient fallback on both")
        return None, None
    return _option_image(picks["a"]), _option_image(picks["b"])


def _describe_pick(c: _Candidate | None) -> str:
    if c is None:
        return "none"
    return f"tier {c.tier} {c.source}: {c.article or c.title}"[:120]
