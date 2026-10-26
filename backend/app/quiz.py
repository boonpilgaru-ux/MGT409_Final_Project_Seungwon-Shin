"""Adaptive balance-game engine (SPEC section 3).

Public API (routes.py):
    await start_quiz(session_id) -> Question        idempotent; generates (or claims) round 1 on first call
    await answer(session_id, answer) -> AnswerResult
    warm(country)                                    pre-generate a round 1 for the landing page's country
    depth_for_round(index) -> Depth
Helpers: ``await shutdown()`` cancels background work (app shutdown / tests); ``await
forget_session(id)``; ``prefetch_keys()``.

How a round is made
-------------------
1. ``_generate(snapshot)`` asks the question model for the next round: an updated hidden hypothesis,
   the dimension to probe, and the A/B question (labels, native-script sublabels, emoji, color,
   image_query, Wikipedia article). Inputs: country, full Q/A history (prompt, both options,
   chosen side, dimension), round index, depth target, whether this round must be locally grounded (keeps >= 2/3 local),
   a per-session shuffled list of dimension ideas, an optional "arena" nudge, and local
   references already used (this game, the omitted style examples of the player's own country,
   and recent games of other players from that country). The system prompt is built per request
   without the player's own country's style examples, so games aren't cloned from them.
   Every third round is a required twist round (would-you-rather, perk with a catch, both
   painful or "forever"; the type comes from a per-session shuffled order).
   The draft is validated (lengths, distinct options, colors, repeated dimension, the same object
   as an earlier round even in other words, safety, reused references, nationality words, labels
   or sublabels that spell out the trait, a twist round without a twist, too many shirt prompts,
   too many "forever" frames, a local reference that isn't on the cards) and regenerated
   once with the problems listed. Hard problems always retry; soft-only problems retry only while
   no player is waiting (``wanted`` event), so a fast player never pays 3-7 s for a nicer draft. A
   prompt that still asks "X or Y?" is cut to its frame, and a "forever" past the per-game quota
   (MAX_FOREVER_PROMPTS, never two rounds in a row) is dropped from the prompt. Labels must name
   the concrete thing (an abstract label, or a sublabel whose English meaning names something else,
   is a soft issue; a mismatched sublabel that survives is dropped). Then ``images.find_pair``
   finds both photos (each option's Wikipedia article lead image, Commons search, one LLM judge
   call), never repeating a photo shown earlier in the game; both cards get a photo or neither
   does (balanced cards).
2. Speculative prefetch: whenever question N becomes current, two background tasks generate N+1
   assuming "A" and assuming "B". They live in ``_prefetch`` keyed by (session_id, question_id,
   choice), work on immutable snapshots, and never write to the session.
3. On answer, the losing branch is cancelled, the matching task is awaited (or a fresh generation
   runs if it is missing/failed, e.g. after a process restart), and the result is committed under
   the session lock. Failures leave the session untouched so the client can retry.
4. Answering the last round (QUIZ_TOTAL) finishes the game; a background task folds that last pick into the hypothesis.
5. Round 1 has no player input, so the landing page warms one per country (``warm``); the first
   ``start_quiz`` of a session claims it (each warm round goes to exactly one session).

Hidden data lives in the session record: ``StoredQuestion.dimension`` / ``rationale`` /
``image_queries``, ``rec.hypothesis``, and ``rec.extra["quiz.local_refs"]`` ({question_id: local
reference or ""}; only references actually shown on the cards count).
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import random
import re
import secrets
import time
import unicodedata
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal

import openai
from pydantic import BaseModel, Field, field_validator

from . import images, llm, store
from .config import get_settings
from .errors import ConflictError, LLMError
from .models import QUIZ_TOTAL, Answer, AnswerResult, Choice, Country, Depth, HexColor, Option, OptionImage, Question
from .store import SessionRecord, StoredAnswer, StoredQuestion

__all__ = ["QUIZ_TOTAL", "depth_for_round", "start_quiz", "answer", "shutdown", "prefetch_keys"]

log = logging.getLogger("teedentity.quiz")

# --------------------------------------------------------------------------- tunables

# "none" skips hidden reasoning: ~4 s per round instead of ~6-10 s, with equally good questions in live
# tests. If the model rejects it, we fall back to "low" for the rest of the process.
REASONING_EFFORT = os.getenv("TEE_QUESTION_EFFORT", "").strip() or "none"
FALLBACK_EFFORT = "low"
QUESTION_MAX_TOKENS = 1500
GENERATION_TIMEOUT_S = 75.0  # one LLM attempt (incl. chat_json's own JSON repair)
HEDGE_AFTER_S = 6.5  # start a parallel second attempt if the first is this slow (normal: ~3-6 s)
IMAGE_BUDGET_S = 6.0  # both option photos (candidates + judge + the parity tier); normally ~2-3 s
WARM_TTL_S = 10 * 60.0  # a pre-generated round 1 (POST /quiz/warm) is served for this long
WARM_MAX = 50  # countries with a warm round 1 at once
PREFETCH_TTL_S = 30 * 60.0  # abandoned sessions: drop speculative results after this
PREFETCH_MAX = 200  # hard cap on registry entries (oldest evicted first)
RECENT_DIMENSIONS = 3
HYPOTHESIS_MAX_WORDS = 60
LOCAL_REFS_KEY = "quiz.local_refs"

LIMITS = {"prompt_words": 12, "label_words": 7, "sublabel_words": 12, "query_words": 5}
CHAR_LIMITS = {"prompt": 90, "label": 50, "sublabel": 60}


def depth_for_round(index: int) -> Depth:
    """Depth target for a 1-based round index: 1-3 broad, 4-7 focused, 8-10 specific."""
    if index <= 3:
        return "broad"
    if index <= 7:
        return "focused"
    return "specific"


def _local_required(index: int, local_so_far: int) -> bool:
    """Keep at least ceil(2/3 * rounds) of the rounds locally grounded."""
    return local_so_far < (2 * index + 2) // 3


# --------------------------------------------------------------------------- prompt material

DEPTH_GUIDE: dict[str, str] = {
    "broad": "famous food fights and childhood pop-culture icons. Dead simple, instantly answerable, funny.",
    "focused": (
        "fandoms (music, dramas, games, sports teams), a famous place, and one joke law. Two famous names, "
        "one plain prompt."
    ),
    "specific": (
        "style and vibe between famous icons: two famous brands, eras, characters or aesthetics of the same kind "
        "(loud vs minimal, retro vs modern, cute vs cool), e.g. 'Your closet: Uniqlo vs Supreme', 'Mascot: Kakao "
        "Friends Ryan vs Line Friends Brown'. Still a fun A-vs-B, never a survey about print size or placement."
    ),
}

# Dimension ideas per depth, offered in a per-session shuffled order (the model tends to take
# the first idea, which made every game open with the same axis).
DIMENSION_IDEAS: dict[str, list[str]] = {
    "broad": [
        "food loyalty (classic order vs chaotic new combo)",
        "comfort (cozy homebody vs out until 3 a.m.)",
        "social energy (whole group chat vs one best friend)",
        "treats (save up vs splurge now)",
        "planning style (chaotic improviser vs color-coded planner)",
        "pettiness (let it go vs keep the receipts)",
    ],
    "focused": [
        "rule maker vs rule breaker (playful made-up laws)",
        "tradition vs trend",
        "humor style (dry deadpan vs unhinged chaos)",
        "status (flex it vs stealth wealth)",
        "mainstream vs niche (fandom choices)",
        "idealist vs pragmatist (big ideas, small stakes)",
        "local pride vs global curiosity",
        "spotlight vs background",
        "risk appetite",
        "nostalgia vs novelty",
    ],
    "specific": [
        "era (a retro decade vs futuristic)",
        "saturation (washed-out vs neon)",
        "palette (monochrome vs multicolor)",
        "typography (hand-lettered vs clean geometric)",
        "script (local script vs Latin letters)",
        "motif world (nature vs urban)",
        "subculture or scene",
        "statement level (loud big graphic vs tiny detail)",
        "visual humor (pun vs earnest art)",
        "silhouette (roomy and slouchy vs sharp and fitted)",
    ],
}

ARENAS: dict[str, list[str]] = {
    "broad": [
        "iconic street food", "convenience-store snacks", "famous fast-food chains", "late-night delivery food",
        "childhood cartoons and anime", "iconic video games",
    ],
    "focused": [
        "TV dramas and variety shows", "sports teams and leagues", "famous cities and places", "movies and franchises",
        "music genres and eras", "famous mascots and characters", "if you ran the country for one day (silly laws)",
    ],
    "specific": [
        "fashion brands and street style", "eras (80s, 90s, Y2K, future)", "cute vs cool characters",
        "minimal vs loud aesthetics", "retro vs modern icons", "sports jerseys and team colors",
    ],
}

# Style examples: (country code or "*", example line, its local references). The system prompt is built
# per request WITHOUT the player's own country's lines (their games used to clone them: every Korean
# game had a "dabang sign vs Seoul Metro" round), and those references are listed as already used.
STYLE_EXAMPLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("KR", '[South Korea, comfort food] "Pick one. Forever." | A: Jjajangmyeon (짜장면) | B: Jjamppong (짬뽕)',
     ("jjajangmyeon vs jjamppong",)),
    ("KR", '[South Korea, both painful] "For the rest of your life:" | A: Long padded coat every summer (한여름 롱패딩) '
           '| B: Short sleeves every winter (한겨울 반팔)', ("padded coat in summer",)),
    ("KR", '[South Korea, social energy] "Friday night:" | A: Noraebang with the whole team (회식 노래방) '
           '| B: Convenience-store ramyeon, alone (편의점 라면)', ("team-dinner noraebang", "convenience-store ramyeon")),
    ("JP", '[Japan, nostalgia vs novelty] "Saturday in Tokyo:" | A: Showa-era kissaten (純喫茶) '
           '| B: Brand-new teamLab show (チームラボ)', ("Showa kissaten", "teamLab")),
    ("JP", '[Japan, era] "Your bedroom wall:" | A: 80s city-pop album cover (シティポップのレコード) '
           '| B: Y2K purikura sticker sheet (プリクラ)', ("city-pop album cover", "purikura")),
    ("BR", '[Brazil, daily rhythm] "Sunday:" | A: First pastel at the 7 a.m. feira (pastel de feira) '
           '| B: Wake at noon, straight to churrasco', ("pastel de feira", "churrasco")),
    ("BR", '[Brazil, saturation] "Beach day, one towel:" | A: Black-and-white Copacabana wave tiles '
           '(calçadão de Copacabana) | B: Neon Carnaval feather costume (fantasia de Carnaval)', ("Copacabana calçadão", "Carnaval feathers")),
    ("MX", "[Mexico, craft vs convenience] \"Tortillas for life:\" | A: Comal tortillas, a 40-minute wait (tortillas "
           "hechas a mano) | B: Tortillería stack, same as everyone's (tortillas de tortillería)", ("handmade tortillas", "tortillería")),
    ("MX", '[Mexico, pattern vs emblem] "Your kitchen wall:" | A: One giant Lotería card (La Lotería) '
           '| B: Papel picado everywhere (papel picado)', ("Lotería card", "papel picado")),
    ("IN", '[India, daily ritual] "Your 4 p.m. fix:" | A: Cutting chai at a tapri (कटिंग चाय) '
           '| B: Filter kaapi in a davara (ஃபில்டர் காபி)', ("cutting chai", "filter kaapi")),
    ("NG", '[Nigeria, color] "Owambe outfit fabric:" | A: Loud Ankara wax-print brights | B: Washed indigo adire (aṣọ àdìrẹ)',
     ("Ankara wax print", "adire")),
    ("US", '[USA, planning] "Road trip:" | A: Color-coded itinerary binder | B: Full tank and a paper map', ()),
    ("GB", '[UK, comfort food] "Only one tonight:" | A: Beans on toast | B: Chippy chips with curry sauce',
     ("beans on toast", "chippy chips")),
    ("DE", '[Germany, late-night eats] "3 a.m. after the club:" | A: Döner kebab from the corner stand (Döner) '
           '| B: Currywurst and fries at the Imbiss (Currywurst mit Pommes)', ("Döner", "Currywurst")),
    ("*", '[Universal, typography] "Your café\'s menu board:" | A: Brush-lettered chalkboard | B: Crisp backlit letter board', ()),
    ("*", '[Universal, statement level] "New phone case:" | A: One huge glossy sticker | B: A tiny embossed initial', ()),
    ("*", '[Universal, perk with a catch] "Would you rather:" | A: A fortune, but a year on a desert island '
          '| B: Your own couch, same old paycheck', ()),
    ("*", '[Universal, social energy] "Concert night:" | A: Front row, alone | B: Back row with best friends', ()),
    ("KR", '[South Korea, childhood anime] "Childhood anime:" | A: Slam Dunk (슬램덩크) | B: Dragon Ball (드래곤볼)',
     ("Slam Dunk", "Dragon Ball")),
    ("JP", '[Japan, games] "Pick one:" | A: Pokemon (ポケモン) | B: Super Mario (スーパーマリオ)', ("Pokemon", "Mario")),
    ("US", '[USA, food] "Late-night order:" | A: In-N-Out | B: Shake Shack', ()),
    ("BR", '[Brazil, football] "Your shirt for life:" | A: Flamengo | B: Corinthians', ("Flamengo", "Corinthians")),
    ("*", '[Universal, style] "Room vibe:" | A: Muji minimal | B: Hello Kitty everything', ()),
    ("*", '[Universal, era] "Pick your era:" | A: 90s hip-hop | B: Y2K pop', ()),
    ("KR", '[South Korea, food fight] "Tangsuyuk is here. Choose your side:" | A: Pour the sauce (부먹) '
           '| B: Dip the sauce (찍먹)', ("bumeok vs jjikmeok",)),
    ("KR", '[South Korea, made-up law] "President for a day. One law:" | A: Mandatory 2 p.m. nap (낮잠 의무화) '
           '| B: Ban on the 2nd round of team dinners (회식 2차 금지)', ("nap law", "hoesik second round")),
    ("JP", '[Japan, fandom war] "Choose your allegiance:" | A: Kinoko no Yama (きのこの山) '
           '| B: Takenoko no Sato (たけのこの里)', ("kinoko vs takenoko",)),
    ("US", '[USA, food fight] "Settle it forever:" | A: A hot dog is a sandwich | B: Cereal is a soup', ()),
    ("US", '[USA, made-up law] "You run Congress for a day:" | A: Tipping banned, prices just honest '
           '| B: Reply-all emails become a misdemeanor', ()),
    ("GB", '[UK, etiquette war] "Making tea for the office:" | A: Milk in first, proudly | B: Teabag left in, forever', ()),
    ("NG", '[Nigeria, rivalry] "Jollof wars, final answer:" | A: Party jollof with the smoky bottom pot '
           '| B: Ghana jollof, just to start chaos', ("party jollof",)),
    ("BR", '[Brazil, regional rivalry] "The eternal debate:" | A: Biscoito | B: Bolacha', ("biscoito vs bolacha",)),
    ("MX", '[Mexico, food fight] "Mexico City rules:" | A: Quesadilla, cheese optional (quesadilla sin queso) '
           '| B: Cheese is mandatory, by law (con queso, obvio)', ("quesadilla sin queso",)),
    ("*", '[Universal, perk with a catch] "Would you rather:" | A: Unlimited free pizza, but only with pineapple '
          '| B: Free coffee for life, but always lukewarm', ()),
)

SYSTEM_PROMPT = """\
You design the questions for Tee:dentity, a playful web app that gets to know someone through a 10-round \
밸런스게임 (balance game), then designs a t-shirt that expresses who they are. You write ONE round at a time.

## What a 밸런스게임 is
A Korean party / internet game: "A vs B - you MUST pick one." The fun is the dilemma:
- Both options are genuinely tempting, or both a little painful, so the pick reveals a priority. Never an obvious answer.
- Short, punchy, concrete and vivid: you can picture each option instantly.
- Twists make it a real 밸런스게임: "for the rest of your life", "would you rather", a perk with a catch ("a \
fortune, but a year on a desert island" vs "life as it is"), or two painful options ("a padded coat all summer" \
vs "short sleeves all winter"). Use a twist in about one round in three: the request names the twist \
for those rounds; set `twist` to the twist you used ("none" for a plain round).
- Deeply local: the best ones use the foods, places, transit, school life, holidays, pop culture, slang and \
everyday rituals that people from that country instantly recognize and argue about.
- Playful, funny and relatable, ranging from light and general to oddly specific.

## Straightforward + famous icons (the #1 rule)
Every round is a dead-simple "A vs B" between two FAMOUS, instantly recognizable things that people love to \
argue about. Anyone from that country should get it in one second and have an opinion.
- Options are the most popular cultural codes: anime/manga and cartoons (Slam Dunk vs Dragon Ball, Doraemon vs \
Crayon Shin-chan), TV dramas and shows, bands and idol groups, games (Pokemon vs Mario), sports teams and \
leagues, iconic foods and snacks, famous brands and chains, cities and famous places, music genres and eras \
(90s hip-hop vs Y2K pop). Prefer franchises, characters, groups, teams, foods and places over individual people; \
never politicians.
- The label is just the name of the thing ("Slam Dunk", "Tteokbokki", "Pokemon", "Seoul vs Busan"), maybe plus a \
2-3 word funny tag. No riddles, no abstract descriptions, no long setups.
- The prompt is short and plain: "Childhood anime:", "Pick one:", "Late-night food:", "Your team:", \
"President for a day, one law:". Twists stay short too ("Forever:", "Would you rather:").
- Both options are the SAME KIND of thing and equally famous: two dramas, two snacks, two idol groups, two \
brands, two cities, two characters (never "a chicken mascot vs The Scream"). Never two rounds in a row on the same \
kind of thing.
- Every option should work as a T-SHIRT THEME: a fandom, food, place, team, music scene, era or style someone \
would happily wear. The picks are used to recommend and design their t-shirt, so the game must reveal what \
they love (fandoms, foods, places, music, sports) and their style (loud vs minimal, retro vs modern, cute vs cool).
- Mix the topics: about 2 food rounds, 3 pop-culture rounds (anime, music, dramas, games, sports), 1 joke-law \
round, 1 place round and 3 style/vibe rounds (still between famous icons: "Street style: Supreme vs Uniqlo", \
"Room vibe: Muji vs Hello Kitty", "Era: 90s vs Y2K").

## Make it HILARIOUS
Write like the funniest friend in the group chat, in the style of viral this-or-that posts on Reddit, X, \
Instagram and TikTok. Every round should make someone laugh, gasp or start an argument with a friend.
- Lean into the country's famous internet debates, fandom wars and food fights (부먹 vs 찍먹, pineapple pizza, "is a hot dog a \
sandwich", biscoito vs bolacha, jollof wars, kinoko vs takenoko), then invent FRESH ones in the same spirit.
- Use absurd specificity, mild chaos and petty stakes ("you can only text in voice notes", "your ringtone is your \
mom's voice forever"), cursed combos, and joke "laws".
- Playful politics = "if you ran the country for a day" hot takes and silly made-up laws and policies (a national \
nap hour, banning reply-all, tipping, daylight saving, a holiday for a snack). Light civic ideas and values (rules \
vs freedom, big ideas vs practicality), never real politics.
- Mix the topics across the game: food, culture and pop culture, vibes and aesthetics, ideas and joke politics.
- Funny never means mean: punch at situations, foods and habits, never at people or groups.
- The joke lives in the labels: each option should be funny on its own. If a round wouldn't make a friend laugh \
out loud, rewrite it. Never two rounds on the same topic (two music rounds, two snack rounds) in a row.

## Style examples (they show the vibe; never reuse them or their references, invent fresh ones)
Format: [country, dimension] prompt | A: label (sublabel) | B: label (sublabel)
{examples}

## The 10-round arc (depth)
- broad (rounds 1-3): famous food fights and childhood pop-culture icons. Instant laughs.
- focused (rounds 4-7): fandoms (music, dramas, games, sports teams), a famous place, and one joke law ("president for a day").
- specific (rounds 8-10): style and vibe between famous icons (brands, eras, characters, aesthetics), plus taste (colors, typography vibe, motifs, eras, subcultures, statement level, \
texture, logos vs none) read through culture and everyday life: a shop sign, an album cover, a jersey, a snack \
wrapper, a room, a festival outfit. Make both options real, picturable things from the culture that embody each \
taste. The t-shirt is designed LATER from these picks, so don't turn rounds into a design survey: at most one or \
two prompts in the whole game may mention the tee, shirt or print.
Questions should get more specific as rounds go on. Vary the prompt frames: don't reuse the opening or \
closing words of recent prompts.

## Choosing the dimension (maximize information)
Each round probes ONE dimension: a personality or taste axis named in 2-6 words, with its two poles in \
parentheses, e.g. "saturation (washed-out vs neon)".
1. Never repeat a dimension already covered, and skip dimensions the hypothesis already states with confidence. \
Prefer the biggest gap in what we know that matters for the profile and the t-shirt.
2. If the latest pick surprised the hypothesis, follow up from a NEW, sharper angle (a differently named \
sub-dimension) and with a DIFFERENT object: never reuse a dish, place, brand or item from an earlier round. If two \
signals conflict, design a question that separates them.
3. Each option must clearly map to one pole of the dimension, so either pick is informative. Let the thing itself \
show the pole; never spell the trait out in a label or sublabel (bad, for "function vs beauty": "Perfectly useful \
storage wall" vs "Beautifully useless art object"; good: "Muji drawer unit" vs "Tower of the Sun replica").

## Cultural grounding
- The country is the key input. When the round requires a local reference, use something specific and REAL from \
that country (a dish, snack, street, neighborhood, transit line, chain store, festival, school ritual, TV or music \
era, sport, slang) that locals in their 20s-30s would grin at. Skip tourist clichés and stereotypes. Only use \
references you are sure exist, with facts you are sure of (what day a TV slot airs, what an event is called).
- Keep both options in the same cultural world (local vs local) so the choice is about taste, not familiarity.
- Let the specific reference carry the locality: never write the country name or nationality in prompts or labels \
(bad: "Saturday plans, Brazilian edition:", "Road trip through America:", "Korean pop sticker") unless it is part \
of a proper name ("Mexico City", "Indian Railways").
- Labels are in English and NAME THE CONCRETE THING itself: the food, place, object or activity the card is \
about, the thing its photo shows. Never an abstract descriptor whose meaning only the sublabel reveals (bad: \
"One unforgettable centerpiece" (무지개 송편) vs "Everyone's favorite classic" (깨송편); good: "Rainbow \
songpyeon" vs "Sesame songpyeon"). Keep local terms in the label in romanized form when natural.
- `sublabel` is the local NAME OF THE SAME THING the label names, written the way locals write it: in the \
country's own script where it has one (label "Tteokbokki at a pojangmacha", sublabel "포장마차 떡볶이"; "Rainbow \
songpyeon", "무지개 송편"), otherwise in the local language's usual spelling ("pastel de feira", "aṣọ òkè"). \
Never a different thing: not a related brand, team, show, guide, event or place (bad: label "Seoul rooftop tasting \
menu", sublabel "미쉐린 가이드 서울"), never the personality trait or the pole (bad: "Big crowd, bigger funnel \
cake"), never a script locals don't use for it (no Arabic or Cyrillic for a Lagos market) and never a made-up \
translation. Null when locals just call it by its English name: always for the US, UK, Ireland, Australia and \
New Zealand, and in Nigeria unless the thing has a real Yoruba, Igbo, Hausa or Pidgin name. `sublabel_en` is the \
sublabel's literal English meaning (null without a sublabel).
- Names must be the standard ones locals actually use, and the romanized label must match the native name exactly. \
Never invent, mistranslate or calque a phrase. If a real item's name contains an off-limits word (e.g. 마약김밥, \
literally "narcotic gimbap"), pick a different item instead of renaming it.
- `local_reference` names the specific local thing shown in the options (it must appear in a label, sublabel or \
image_query), or null if the round is universal. Make the options themselves BE that thing; never paste the \
reference into a sublabel just so it appears (a sublabel only ever names its own option, and the two sublabels \
are never identical).

## Balance
- No socially "correct" answer: neither option healthier, kinder, smarter, richer or cooler. Both a real treat \
or a real sacrifice. If one side is the "handmade" or "authentic" one, give it a cost too ("handmade, but a \
40-minute wait" vs "instant, but everyone has the same one").
- Parallel form: same grammatical shape, similar length and specificity. Never a negation of the other option.
- The prompt must fit BOTH options (a "Saturday morning:" frame can't hold "Pancakes after midnight").

## Length limits (hard)
- prompt: <= 12 words, ideally 2-6. A short frame like "Saturday night:", "3 a.m., starving:", "Your desk:", \
"Would you rather:", "Pick one. Forever.". Never restate or ask about the options in it (bad: "Plan every stop or \
wander?"). "Forever" / "for life" is ONE twist among several: use it in at most 3 rounds of a game, never two rounds \
in a row, and never as a default ending ("Your X, forever:" every round gets boring fast).
- label: <= 7 words (ideally 2-5). Punchy noun phrases in sentence case (capitalize only the first word and \
proper nouns), not sentences.
- sublabel: <= 12 words (ideally <= 6), or null. For countries with a non-Latin script, write it in that script.

## Safety (hard)
Playful made-up laws and civic hot takes are welcome, but never real politicians, parties, elections, \
governments' actual policies, or hot-button issues (abortion, guns, immigration, war, territorial disputes). \
No religion, sexual content, drugs, heavy drinking, violence, tragedies or disasters, illness, body \
shaming, or stereotypes about any group. Never infer personality from nationality. Keep it light, kind and hilarious: \
something that gets a whole friendly dinner table yelling and laughing.

## Per-option extras
- image_query: 2-5 English words naming the one concrete, photographable subject of the option: the specific \
subject plus its category noun, the way a Wikimedia Commons photo title would name it, proper nouns capitalized \
("night market stall", "cassette tape deck", "neon shop sign", "ceramic tea bowl", "Arc de Triomphe"). Photo \
search matches EVERY word, so no scene, mood, time-of-day, style or color words (the card's style lives in the \
label; the photo just shows the thing: "colorful transit card" -> "transit card", "fine-line tteokbokki sketch" \
-> "tteokbokki"), and never a lone ambiguous noun ("arcade" -> "arcade game cabinet", "crane" -> "claw crane \
machine", "chips" -> "potato chips bag"). Bad: "friends eating at a stall at night", "cozy morning vibes". No \
people's names or logos. It names the same thing as the label.
- wiki_lang + wiki_title: the Wikipedia article whose lead photo best shows the option's thing: an exact, existing \
article title, the most specific one about the thing itself (the dish, not the cuisine; the market, not the city; \
the object, not its brand or maker). Prefer the local-language Wikipedia for local things (ko + "빈대떡", pt + \
"Pão de queijo") and English for global ones (en + "Cassette tape"). Nearly every concrete thing \
has one: if no article is about exactly this, use the closest one whose photo shows it (en + "Karaoke box", en + \
"Board game"). For a franchise, team, group or brand use its own article (en + "Slam Dunk (manga)"). Never a real person or a disambiguation page.
- emoji: exactly one emoji for the option.
- color: a #RRGGBB hex that evokes the option (fallback card gradient). Make the two colors clearly different.

## Hidden hypothesis
Return an updated running hypothesis (<= 60 words, compact notes): traits, tensions, taste leanings, and what is \
still unknown. Integrate ALL picks so far, especially the latest. In round 1 (no picks yet) note what you'll \
probe first. The user never sees it.
"""


def _system_prompt(country: Country) -> str:
    """SYSTEM_PROMPT with the style examples of every country except the player's own."""
    lines = [f"- {line}" for code, line, _ in STYLE_EXAMPLES if code != country.code]
    return SYSTEM_PROMPT.format(examples="\n".join(lines))


def _example_refs(country: Country) -> list[str]:
    """References of the player's own country's (omitted) examples: treat them as already used."""
    return [ref for code, _, refs in STYLE_EXAMPLES if code == country.code for ref in refs]

HYPOTHESIS_SYSTEM = """\
You keep a hidden running hypothesis about someone who just finished a 10-round 밸런스게임 (A-vs-B balance \
game) in a personalized t-shirt app. Update it with the full history, especially the final pick. Write <= 60 \
words of compact notes: personality traits, tensions, and taste leanings (colors, eras, motifs, fit, statement \
level). Never infer personality from nationality alone.
"""


# --------------------------------------------------------------------------- LLM output schemas


class GenOption(BaseModel):
    label: str = Field(description="English, <= 7 words, names the concrete thing itself")
    sublabel: str | None = Field(
        default=None, description="native-script / local-language name of the SAME thing, <= 12 words, or null"
    )
    sublabel_en: str | None = Field(default=None, description="literal English meaning of the sublabel, or null")
    emoji: str = Field(description="exactly one emoji")
    color: HexColor = Field(description="#RRGGBB evoking the option")
    image_query: str = Field(description="2-5 English words, concrete and photographable, subject first")
    wiki_lang: str | None = Field(default=None, description="language code of the Wikipedia with wiki_title (ko, ja, pt, en...)")
    wiki_title: str | None = Field(
        default=None, description="exact title of the Wikipedia article whose lead photo shows this thing, or null"
    )


Twist = Literal["none", "would_you_rather", "perk_with_catch", "both_painful", "forever"]

# Twist rounds (every third round) get one of these, from a per-session shuffled order.
TWISTS: dict[str, str] = {
    "would_you_rather": 'a "Would you rather:" hypothetical in which each option comes with a real cost',
    "perk_with_catch": (
        'a perk with a catch: each option is a treat that comes with a real cost ("a fortune, but a year on a '
        'desert island" vs "life exactly as it is")'
    ),
    "both_painful": (
        "both options a little painful, so the pick shows which sacrifice they'd rather make (\"a padded coat "
        'all summer" vs "short sleeves all winter")'
    ),
    "forever": '"forever" / "for the rest of your life": two tempting things, and they must live with the pick',
}


class GenQuestion(BaseModel):
    hypothesis: str = Field(description="updated hidden hypothesis about the person, <= 60 words")
    dimension: str = Field(description="the ONE axis this round probes, 2-6 words")
    why: str = Field(default="", description="<= 25 words: why this dimension now")
    local_reference: str | None = Field(default=None, description="specific local thing used, or null if universal")
    twist: Twist = Field(default="none", description="the twist this round uses, or none")
    prompt: str = Field(description="<= 12 words")
    option_a: GenOption
    option_b: GenOption

    @field_validator("twist", mode="before")
    @classmethod
    def _known_twist(cls, value: Any) -> str:
        """Map free-form answers ("Perk with a catch", "would-you-rather") onto the enum instead of
        failing validation (which would cost a JSON-repair round trip)."""
        text = re.sub(r"[\W_]+", " ", str(value or "").casefold())
        for key, hint in (("forever", "forever"), ("would_you_rather", "rather"), ("perk_with_catch", "catch"),
                          ("perk_with_catch", "perk"), ("both_painful", "pain")):
            if hint in text:
                return key
        return "none"


class HypothesisUpdate(BaseModel):
    hypothesis: str = Field(description="updated hidden hypothesis, <= 60 words")


# --------------------------------------------------------------------------- snapshots & results


@dataclass(frozen=True)
class _Snapshot:
    """Immutable view of everything generation needs (safe to hand to background tasks)."""

    session_id: str
    country: Country
    answers: tuple[StoredAnswer, ...]
    hypothesis: str
    local_refs: dict[str, str] = field(default_factory=dict)

    @property
    def next_index(self) -> int:
        return len(self.answers) + 1

    @classmethod
    def of(cls, rec: SessionRecord, assume: Choice | None = None) -> _Snapshot:
        """Snapshot of ``rec``; with ``assume``, as if the current question were answered that way."""
        answers = list(rec.answers)
        if assume is not None:
            if rec.current is None:
                raise ValueError("no current question to assume an answer for")
            answers.append(_answered(rec.current, assume))
        refs = rec.extra.get(LOCAL_REFS_KEY)
        return cls(
            session_id=rec.id,
            country=rec.country.model_copy(),
            answers=tuple(a.model_copy(deep=True) for a in answers),
            hypothesis=rec.hypothesis,
            local_refs=dict(refs) if isinstance(refs, dict) else {},
        )


@dataclass(frozen=True)
class _Generated:
    stored: StoredQuestion
    hypothesis: str
    local_ref: str  # "" when universal


def _answered(current: StoredQuestion, choice: Choice) -> StoredAnswer:
    return StoredAnswer(
        question=current.question, dimension=current.dimension, choice=choice, image_queries=current.image_queries
    )


# --------------------------------------------------------------------------- prompt building


def _clean(text: str | None) -> str:
    return " ".join(str(text or "").split()).strip().strip('"').strip()


def _norm(text: str | None) -> str:
    return re.sub(r"[\W_]+", " ", str(text or "").casefold()).strip()


def _fmt_option(option: Option) -> str:
    return f"{option.label} ({option.sublabel})" if option.sublabel else option.label


_DIM_FILLER = {"vs", "versus", "and", "or", "the", "a", "an", "of", "to", "for", "with", "level", "style"}


def _dim_tokens(dimension: str) -> frozenset[str]:
    head = dimension.split("(")[0]
    return frozenset(t for t in _norm(head).split() if t not in _DIM_FILLER)


def _dims_similar(a: str, b: str) -> bool:
    ta, tb = _dim_tokens(a), _dim_tokens(b)
    if not ta or not tb:
        return _norm(a) == _norm(b)
    return ta == tb or len(ta & tb) / len(ta | tb) >= 0.6


# Every game gets these crowd-pleasers at fixed rounds; the rest rotate per session.
FIXED_ARENAS: dict[int, str] = {
    1: "the country's most famous FOOD FIGHT between two iconic dishes or snacks everyone knows",
    2: "the most famous childhood anime / cartoon / manga rivalry (e.g. Slam Dunk vs Dragon Ball)",
    4: "music fandom: two hugely famous artists, idol groups, bands or genres from the country",
    5: "president for a day: two silly made-up laws or policies (absurd but tempting), never real politics",
    8: "street style: two famous fashion brands or style icons people in the country wear",
}


def _arena(session_id: str, index: int, depth: Depth) -> str:
    """Per-session shuffled arena rotation, so two players from one country get different games."""
    if index in FIXED_ARENAS:
        return FIXED_ARENAS[index]
    order = list(ARENAS[depth])
    random.Random(f"{session_id}:{depth}").shuffle(order)
    start = {"broad": 1, "focused": 4, "specific": 8}[depth]
    return order[(index - start) % len(order)]


def _dimension_order(session_id: str, depth: Depth) -> list[str]:
    """DIMENSION_IDEAS for ``depth`` in a per-session shuffled order (seeded like ``_arena``)."""
    order = list(DIMENSION_IDEAS[depth])
    random.Random(f"{session_id}:dimensions:{depth}").shuffle(order)
    return order


# Local references recently used by other players, per country: offered as "prefer different
# ones" so groups comparing their games (or a repeat player) don't all get the same Gwangjang
# Market round. In memory only; it just nudges variety.
RECENT_REFS_PER_COUNTRY = 20
_recent_refs: dict[str, deque[str]] = {}


def _remember_ref(country_code: str, ref: str) -> None:
    if ref:
        recent = _recent_refs.setdefault(country_code, deque(maxlen=RECENT_REFS_PER_COUNTRY))
        if ref not in recent:
            recent.append(ref)


_TEE_RE = re.compile(r"\b(?:tees?|t shirts?|tshirts?|shirts?|chest)\b")
MAX_TEE_PROMPTS = 2  # prompts per game that may mention the shirt (the design is inferred later)


def _tee_prompts(snap: _Snapshot) -> int:
    return sum(1 for a in snap.answers if _TEE_RE.search(_norm(a.question.prompt)))


# "Forever" is the easiest twist, and once the model is nudged toward twists it ends prompt after
# prompt with it ("Your desk relic, forever:", "Snack wrapper, forever:" ... 11 of 15 in one live
# game). Allow a few per game, never back to back.
_FOREVER_RE = re.compile(r"\b(?:forever|for life|rest of your life)\b")
MAX_FOREVER_PROMPTS = 3


def _forever_prompts(snap: _Snapshot) -> int:
    return sum(1 for a in snap.answers if _FOREVER_RE.search(_norm(a.question.prompt)))


def _forever_used_up(snap: _Snapshot) -> bool:
    """True when this round may not use a "forever" frame (quota reached, or the last round had one)."""
    last = snap.answers[-1].question.prompt if snap.answers else ""
    return _forever_prompts(snap) >= MAX_FOREVER_PROMPTS or bool(_FOREVER_RE.search(_norm(last)))


_FOREVER_TAIL_RE = re.compile(r"[\s,.;:!?–—-]*\bforever\b[\s.,;:!?–—-]*$", re.IGNORECASE)
_YOUR_FOREVER_RE = re.compile(r"\b(your|my|one)\s+forever\s+(?=\w)", re.IGNORECASE)


def _drop_forever(prompt: str) -> str:
    """Remove a trailing or adjectival "forever": "Your desk relic, forever:" -> "Your desk relic:",
    "Pick one. Forever." -> "Pick one:", "Your forever game-day look:" -> "Your game-day look:".
    Mid-sentence uses ("Forever stuck with:") are left alone (the soft check asks for a rewrite)."""
    text = _YOUR_FOREVER_RE.sub(lambda m: f"{m.group(1)} ", prompt)
    match = _FOREVER_TAIL_RE.search(text)
    if match:
        head = text[: match.start()].rstrip(" ,.;:!?–—-")
        text = f"{head}:" if head else "Pick one:"
    return text


def _twist_for(snap: _Snapshot) -> str | None:
    """The twist the next round must use (every third round), else None. Per-session shuffled order
    (seeded like ``_arena``); "forever" is skipped while it is used up."""
    index = snap.next_index
    if index % 3:
        return None
    order = list(TWISTS)
    random.Random(f"{snap.session_id}:twists").shuffle(order)
    if _forever_used_up(snap):
        order.remove("forever")
    return order[(index // 3 - 1) % len(order)]


def _history_lines(snap: _Snapshot) -> list[str]:
    lines = []
    for a in snap.answers:
        q = a.question
        lines.append(
            f'R{q.index} [{q.depth} | {a.dimension}] "{q.prompt}" A: {_fmt_option(q.option_a)} / '
            f"B: {_fmt_option(q.option_b)} -> picked {a.choice} ({a.chosen.label})"
        )
    return lines


def _country_label(country: Country) -> str:
    """'South Korea (KR)'. The name comes from the client, so keep only name-like characters."""
    name = re.sub(r"[^\w\s().,'’&-]", "", _clean(country.name))[:80].strip() or country.code
    return f"{name} ({country.code})"


def _build_user_prompt(snap: _Snapshot, must_local: bool) -> str:
    index = snap.next_index
    depth = depth_for_round(index)
    country = _country_label(snap.country)
    covered = [a.dimension for a in snap.answers]
    ideas = [d for d in _dimension_order(snap.session_id, depth) if not any(_dims_similar(d, c) for c in covered)]
    used_refs = sorted({r for r in snap.local_refs.values() if r})
    used_keys = {_norm(r) for r in used_refs}
    others = [r for r in reversed(_recent_refs.get(snap.country.code, ())) if _norm(r) not in used_keys][:12]
    parts = [
        f"Country: {country}",
        f"Round {index} of {QUIZ_TOTAL}. Depth target: {depth} - {DEPTH_GUIDE[depth]}",
        (
            f"Local grounding: REQUIRED this round. Use a specific, real reference from {country}."
            if must_local
            else "Local grounding: optional this round (a universal question is fine; local is still welcome)."
        ),
        f"Optional arena for variety (ignore it if it doesn't fit the dimension): {_arena(snap.session_id, index, depth)}",
        "",
        f"Running hypothesis: {snap.hypothesis or '(none yet: this is the first round)'}",
        "",
        "History (oldest first):",
        *(_history_lines(snap) or ["(no answers yet)"]),
    ]
    if snap.answers:
        last = snap.answers[-1]
        parts += [
            "",
            f'Latest pick: "{last.chosen.label}" over "{last.rejected.label}" (dimension: {last.dimension}). '
            "Decide whether it confirms or surprises the hypothesis, then update it.",
            f"Dimensions already covered (do not repeat): {'; '.join(covered)}",
        ]
        parts.append(
            "Never reuse a dish, place, brand or item from the history above; a follow-up uses a different object."
        )
    if index == 1 and ideas:
        # Nothing is known yet, so any axis is as informative as another: take this player's first
        # (shuffled) idea, otherwise nearly every game opens with "social energy".
        parts.append(f"Round 1 dimension: {ideas[0]}")
    else:
        parts.append(
            "Unexplored dimension ideas for this depth (prefer the earliest one that fits; this order is shuffled "
            f"per player): {'; '.join(ideas) or 'invent a sharper one'}"
        )
    used = used_refs + [r for r in _example_refs(snap.country) if _norm(r) not in used_keys]
    if used:
        parts.append(f"Local references already used (pick new ones): {'; '.join(used)}")
    if others:
        parts.append(f"Recently used by other players from {country} (prefer different ones): {'; '.join(others)}")
    if _tee_prompts(snap) >= MAX_TEE_PROMPTS:
        parts.append("Earlier prompts already mentioned the shirt enough: frame this one as a life or culture dilemma.")
    no_forever = _forever_used_up(snap)
    if no_forever:
        parts.append("Don't use 'forever' or 'for life' in this prompt: earlier rounds used it enough.")
    if twist := _twist_for(snap):
        parts.append(
            f"Twist round (required): {TWISTS[twist]}. Neither side may be the obviously better deal: each option "
            f'carries a cost, or both are a little painful. Set twist to "{twist}".'
        )
    parts += ["", f"Write round {index} now."]
    return "\n".join(parts)


# --------------------------------------------------------------------------- validation

# Safety net behind the prompt rules: unambiguous off-limits words only, so harmless phrases
# ("kill time", "bath bomb", "water gun", "Cancer vs Leo") don't cost a regeneration.
_UNSAFE_RE = re.compile(
    r"\b(election\w*|parliament|senat\w*|communis\w*|fascis\w*|religio\w*|god|gods|jesus|allah|"
    r"church\w*|mosque\w*|bible|quran|sex|sexy|sexual\w*|nude\w*|naked|porn\w*|drugs?|cocaine|suicid\w*|"
    r"terroris\w*|warfare|murder\w*|nazi\w*|racis\w*|funeral\w*|pandemic|covid)\b",
    re.IGNORECASE,
)


def _words(text: str | None) -> int:
    return len(str(text or "").split())


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)


def _problems(draft: GenQuestion, snap: _Snapshot, must_local: bool) -> tuple[list[str], list[str]]:
    """Rule violations as (hard, soft). Hard = unusable; soft = acceptable if a retry doesn't fix it."""
    hard: list[str] = []
    soft: list[str] = []
    a, b = draft.option_a, draft.option_b

    if not _clean(draft.prompt):
        hard.append("prompt is empty")
    else:
        if _words(draft.prompt) > LIMITS["prompt_words"] or len(_clean(draft.prompt)) > CHAR_LIMITS["prompt"]:
            (hard if _words(draft.prompt) > LIMITS["prompt_words"] + 4 else soft).append(
                f"prompt has {_words(draft.prompt)} words (max {LIMITS['prompt_words']})"
            )
        if re.search(r"\bor\b", _norm(draft.prompt)):
            soft.append('prompt asks "X or Y?"; use a short frame and let the two cards be the choice')
        words = _norm(draft.prompt).split()
        for previous in reversed(snap.answers[-4:]):
            before = _norm(previous.question.prompt).split()
            if len(words) >= 3 and len(before) >= 3 and (words[:3] == before[:3] or words[-3:] == before[-3:]):
                soft.append(f'prompt frame repeats round {previous.question.index} ("{previous.question.prompt}"); vary it')
                break
    for side, opt in (("A", a), ("B", b)):
        label, sublabel = _clean(opt.label), _clean(opt.sublabel)
        if not label:
            hard.append(f"option {side} label is empty")
            continue
        if _words(label) > LIMITS["label_words"] or len(label) > CHAR_LIMITS["label"]:
            (hard if _words(label) > LIMITS["label_words"] + 3 else soft).append(
                f"option {side} label has {_words(label)} words / {len(label)} chars (max {LIMITS['label_words']} words)"
            )
        if sublabel and (_words(sublabel) > LIMITS["sublabel_words"] or len(sublabel) > CHAR_LIMITS["sublabel"]):
            soft.append(f"option {side} sublabel is too long (max {LIMITS['sublabel_words']} words)")
        if not _clean(opt.emoji):
            soft.append(f"option {side} needs an emoji")
        if _words(opt.image_query) == 0 or _words(opt.image_query) > LIMITS["query_words"]:
            soft.append(f"option {side} image_query must be 2-5 words")

    if _norm(a.label) and _norm(a.label) == _norm(b.label):
        hard.append("both options have the same label; they must be different choices")
    elif _norm(a.image_query) and _norm(a.image_query) == _norm(b.image_query):
        soft.append("both options use the same image_query; each needs its own subject")
    if sum(abs(x - y) for x, y in zip(_rgb(a.color), _rgb(b.color))) < 90:
        soft.append("the two option colors are nearly identical; make them clearly different")

    english = " ".join(_clean(t) for t in (draft.prompt, a.label, b.label, a.sublabel, b.sublabel, a.image_query, b.image_query))
    if bad := sorted({m.group(0).lower() for m in _UNSAFE_RE.finditer(english)}):
        hard.append(f"off-limits topic words ({', '.join(bad)}); keep it light and non-political/non-religious")

    if not _clean(draft.dimension):
        soft.append("dimension is empty")
    else:
        recent = [x.dimension for x in snap.answers[-RECENT_DIMENSIONS:]]
        if any(_dims_similar(draft.dimension, d) for d in recent) or any(
            _norm(draft.dimension) == _norm(x.dimension) for x in snap.answers
        ):
            soft.append(f'dimension "{draft.dimension}" was already covered; probe a different axis')

    previous_labels = {_norm(o.label) for x in snap.answers for o in (x.question.option_a, x.question.option_b)}
    if {_norm(a.label), _norm(b.label)} & previous_labels:
        soft.append("an option repeats one from an earlier round; use fresh options")
    if repeat := _repeated_thing(draft, snap):
        soft.append(
            f"an option shows the same thing as an earlier round: {repeat[0]!r} again (round {repeat[1]}: "
            f"{repeat[2]!r}); pick a different subject, not another take on {repeat[0]!r}"
        )

    if _TEE_RE.search(_norm(draft.prompt)) and _tee_prompts(snap) >= MAX_TEE_PROMPTS:
        soft.append("earlier prompts already mention the shirt; frame this one as a life or culture dilemma")
    if _FOREVER_RE.search(_norm(draft.prompt)) and _forever_used_up(snap):
        soft.append(
            "earlier prompts already used 'forever' / 'for life' enough; use a different frame or twist "
            "(e.g. 'would you rather', a perk with a catch, two painful options, or a plain scene)"
        )
    # Identical or pasted-in sublabels (see _pasted_sublabel) are NOT soft issues: _tidy drops them
    # deterministically, and as retry triggers they doubled the soft retries in live games (which
    # delays prefetches and makes players wait for photos). A pasted reference then simply doesn't
    # count toward the local quota, so a later round gets the local requirement instead.
    # Labels: personality / value rounds only. Specific rounds are about visual taste, and naming the
    # look on a real thing is the point there ("Neon Carnaval feathers", "Crisp konbini lettering").
    stems = _trait_stems(draft.dimension) if depth_for_round(snap.next_index) != "specific" else frozenset()
    poles = _pole_words(draft.dimension)
    for side, opt in (("A", a), ("B", b)):
        if _names_trait(opt.label, stems):
            soft.append(
                f"option {side} label spells out the trait ({opt.label!r}); name a concrete thing that embodies "
                "the pole instead of describing the pole"
            )
        if poles and opt.sublabel and set(_norm(opt.sublabel).split()) & poles:
            soft.append(
                f"option {side} sublabel spells out the trait ({opt.sublabel!r}); use the native name of the thing "
                "or one concrete detail of it"
            )
        if _foreign_script(opt.sublabel, snap.country):
            soft.append(
                f"option {side} sublabel {opt.sublabel!r} is in a script locals don't use for it; write the local "
                "name the way locals write it, or null"
            )
        if _abstract_label(opt):
            soft.append(
                f"option {side} label {opt.label!r} doesn't name the thing on the card: name the food, place, object "
                'or activity itself (e.g. "Rainbow songpyeon", not "One unforgettable centerpiece"), the same thing '
                "its image_query and wiki_title show"
            )
        elif _sublabel_mismatch(opt):
            soft.append(
                f"option {side} sublabel {opt.sublabel!r} ({opt.sublabel_en or 'no English meaning'}) names something "
                f"other than the label {opt.label!r}: make it the local name of that same thing, or null"
            )
    if (twist := _twist_for(snap)) and draft.twist == "none":
        soft.append(f'this is a twist round: use {TWISTS[twist]}, and set twist to "{twist}"')
    if nationality := [w for t in (draft.prompt, a.label, b.label) for w in _country_words(_clean(t), snap.country)]:
        soft.append(
            f"don't write the country or nationality ({', '.join(nationality)}); let the local reference carry it"
        )

    ref = _norm(draft.local_reference)
    if must_local and not ref:
        soft.append("this round REQUIRES a specific local reference (set local_reference)")
    elif must_local and not _ref_in_options(draft):
        soft.append(
            f'local_reference "{draft.local_reference}" must name the local thing the options actually show: make an '
            "option BE that thing (don't just paste its name into a sublabel), or pick a reference that is on the cards"
        )
    elif ref and ref in {_norm(r) for r in snap.local_refs.values() if r}:
        soft.append(f'local reference "{draft.local_reference}" was already used; pick a new one')
    return hard, soft


_POLE_FILLER = _DIM_FILLER | {"versus", "very", "more", "less", "much", "every", "total", "all", "big", "one"}


def _pole_words(dimension: str) -> frozenset[str]:
    """Content words of the dimension's poles: "social energy (big crowd vs small circle)" ->
    {crowd, small, circle}. A sublabel that repeats them describes the trait instead of the thing."""
    poles = " ".join(re.findall(r"\(([^)]*)\)", dimension or ""))
    return frozenset(w for w in _norm(poles).split() if len(w) >= 4 and w not in _POLE_FILLER)


def _trait_stems(dimension: str) -> frozenset[str]:
    """5-letter stems of the dimension's pole words: the parenthesised poles ("social energy (big
    crowd vs small circle)" -> crowd, small, circl) and a head that names its own poles ("function
    vs beauty" -> funct, beaut). A category head ("social energy") is not a pole."""
    head = (dimension or "").split("(")[0]
    poles = " ".join(re.findall(r"\(([^)]*)\)", dimension or ""))
    if re.search(r"\b(?:vs|versus)\b", _norm(head)):
        poles = f"{poles} {head}"
    return frozenset(w[:5] for w in _norm(poles).split() if len(w) >= 4 and w not in _POLE_FILLER)


# Words that rate or frame a thing without naming it. A label made only of these (and grammar
# words) is abstract: live game, a songpyeon round served "One unforgettable centerpiece" (무지개
# 송편) vs "Everyone's favorite classic" (깨송편), the concrete thing only in the sublabels.
_VAGUE_WORDS = {
    "a", "an", "the", "of", "with", "and", "or", "in", "on", "at", "for", "to", "from", "by", "s", "your", "my",
    "our", "their", "its", "his", "her", "this", "that", "one", "ones", "every", "all", "no", "zero", "only", "just",
    "still", "exactly", "own", "same", "usual", "whole", "entire", "classic", "classics", "favorite", "favourite",
    "favorites", "unforgettable", "everyone", "everybody", "everything", "something", "anything", "thing", "things",
    "centerpiece", "centrepiece", "choice", "option", "pick", "experience", "vibe", "vibes", "moment", "moments",
    "treat", "staple", "icon", "iconic", "legend", "legendary", "original", "signature", "special", "famous",
    "beloved", "best", "perfect", "ultimate", "fancy", "simple", "plain", "basic", "real", "true", "total", "full",
    "new", "brand", "timeless", "trendy", "little", "big", "huge", "tiny", "giant", "lifetime", "forever", "version",
    "take", "kind", "type", "way", "energy", "mood", "aesthetic", "statement", "unique", "rare", "secret", "mystery",
    "dream", "dreamy", "wild", "safe", "bold", "quiet", "loud", "proper", "good", "great", "old", "favorite's",
}


_GRAMMAR_WORDS = {
    "a", "an", "the", "of", "with", "and", "or", "in", "on", "at", "for", "to", "from", "by", "s", "de", "da", "do",
    "das", "dos", "del", "la", "las", "los", "el", "le", "les", "di", "no", "e", "y",
}


def _content_words(text: str | None, vague: bool = True) -> list[str]:
    """Folded words of ``text`` that can name a thing (grammar words dropped, and vague words too
    unless ``vague`` is False)."""
    skip = _VAGUE_WORDS | _GRAMMAR_WORDS if vague else _GRAMMAR_WORDS
    return [w for w in _norm(text).split() if w not in skip and not w.isdigit()]


def _word_match(a: str, b: str) -> bool:
    """Same word up to inflection or romanization drift ("cheer"/"cheering", "jjajangmyeon"/"jajangmyeon")."""
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and (a.startswith(b[:5]) or b.startswith(a[:5])):
        return True
    return min(len(a), len(b)) >= 6 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _latin(text: str | None) -> str:
    return "" if not text or _has_native_script(text) else text


def _abstract_label(option: GenOption) -> bool:
    """Does the label fail to name a concrete thing? Either it is only vague words ("Everyone's
    favorite classic"), or none of its words appear in what the card's photo shows (image_query,
    a Latin-script wiki title or sublabel, the sublabel's English meaning)."""
    words = _content_words(option.label)
    if not words:
        return True
    shown = _content_words(
        " ".join((option.image_query, _latin(option.wiki_title), _latin(option.sublabel), option.sublabel_en or "")),
        vague=False,
    )
    return bool(shown) and not any(_word_match(w, s) for w in words for s in shown)


def _lowercase_somewhere(word: str, texts: tuple[str, ...], anywhere: str) -> bool:
    """Is ``word`` a common noun: written lowercase in one of ``texts`` (after their first word, which
    sentence case capitalizes anyway) or anywhere in ``anywhere`` (the photo query, where only proper
    nouns are capitalized)?"""
    for text, skip_first in ((t, True) for t in texts):
        raw = re.findall(r"[^\W_]+", text or "")
        for i, token in enumerate(raw):
            if (i or not skip_first) and token[:1].islower() and _word_match(_norm(token), word):
                return True
    return any(t[:1].islower() and _word_match(_norm(t), word) for t in re.findall(r"[^\W_]+", anywhere or ""))


def _sublabel_mismatch(option: GenOption) -> bool:
    """Does the sublabel name a different thing than the label? Compared through its English
    meaning (``sublabel_en``, plus the sublabel itself if it is in Latin letters) against the label,
    photo query and wiki title. A shared lone proper noun doesn't make them the same thing: live
    game, "Seoul rooftop tasting menu" / "미쉐린 가이드 서울" (Michelin Guide Seoul) share only "Seoul".
    Without an English meaning there is nothing reliable to compare ("Flohmarkt am Mauerpark" is
    the "Mauerpark flea-market wander"), so it passes."""
    if not option.sublabel or not option.sublabel_en:
        return False
    meaning = " ".join(t for t in (option.sublabel_en, _latin(option.sublabel)) if t)
    gloss = _content_words(meaning, vague=False)
    if not gloss:
        return False
    target = _content_words(" ".join((option.label, option.image_query, _latin(option.wiki_title))), vague=False)
    shared = {w for w in gloss if any(_word_match(w, t) for t in target)}
    if len(shared) >= 2:
        return False
    texts = (option.label, option.sublabel_en or "", _latin(option.sublabel))
    return not any(_lowercase_somewhere(w, texts, option.image_query) for w in shared)


def _drop_mismatched_sublabels(draft: GenQuestion) -> GenQuestion:
    """Backstop when the corrective retry was skipped or didn't fix it: a sublabel naming another
    thing is worse than none (the label carries the card)."""
    def fix(o: GenOption) -> GenOption:
        return o.model_copy(update={"sublabel": None, "sublabel_en": None}) if _sublabel_mismatch(o) else o

    return draft.model_copy(update={"option_a": fix(draft.option_a), "option_b": fix(draft.option_b)})


def _names_trait(text: str | None, stems: frozenset[str]) -> bool:
    """Does a label use a pole word ("Beautifully useless art object" for "function vs beauty")?
    Capitalised words after the first are proper names ("Mexico City taqueria") and don't count."""
    if not text or not stems:
        return False
    words = text.split()
    for i, raw in enumerate(words):
        if i and raw[:1].isupper():
            continue
        if any(len(w) >= 4 and w.startswith(stem) for w in _norm(raw).split() for stem in stems):
            return True
    return False


# Words too generic to make two things "the same" ("neon shop sign" vs "neon street sign").
_THING_FILLER = {
    "the", "and", "with", "for", "from", "your", "one", "two", "de", "del", "la", "las", "los", "el", "le", "les",
    "da", "do", "das", "dos", "di", "shop", "store", "sign", "street", "market", "stall", "food", "style", "plate",
    "bowl", "cup", "bottle", "packet", "pack", "package", "box", "bag", "poster", "card", "game", "night", "day",
    "city", "set", "photo", "view", "old", "new", "little", "small", "big", "tiny", "giant", "classic", "vintage",
    "retro", "modern", "local", "traditional", "hand", "painted", "made", "front", "back", "wall", "corner",
}


def _thing_stems(text: str | None) -> set[str]:
    return {w[:5] for w in _norm(text).split() if len(w) >= 3 and not w.isdigit() and w not in _THING_FILLER}


def _proper_bigrams(text: str | None) -> set[str]:
    """Adjacent capitalised word pairs ("Welcome to Fabulous Las Vegas" -> {"fabulous las", "las vegas"}),
    including a name plus number ("Route 66 vacancy sign" -> {"route 66"})."""
    words = [w for w in re.split(r"[\s\-–—,.:;!?()/]+", str(text or "")) if w]
    return {
        _norm(f"{x} {y}") for x, y in zip(words, words[1:])
        if x[:1].isupper() and x[:1].isalpha() and ((y[:1].isupper() and y[:1].isalpha()) or y.isdigit())
    }


def _shared_subject(a: str | None, b: str | None) -> str:
    """What two option names / photo queries both show ("" if they show different things): the same
    text, a shared multi-word proper name ("las vegas" in "Las Vegas Strip" / "Welcome to Fabulous Las
    Vegas"), or 2+ shared content words ("lucha libre" in "Máscara de lucha libre" / "Lucha libre",
    "school emblem" in "school jacket emblem" / "school emblem patch")."""
    if not _norm(a) or not _norm(b):
        return ""
    if _norm(a) == _norm(b):
        return _norm(a)
    if names := sorted(_proper_bigrams(a) & _proper_bigrams(b)):
        return names[0]
    shared = _thing_stems(a) & _thing_stems(b)
    if len(shared) < 2:
        return ""
    return " ".join(dict.fromkeys(w for w in _norm(a).split() if w[:5] in shared and w not in _THING_FILLER))


def _same_thing(a: str | None, b: str | None) -> bool:
    return bool(_shared_subject(a, b))


def _repeated_thing(draft: GenQuestion, snap: _Snapshot) -> tuple[str, int, str] | None:
    """(shared subject, earlier round, earlier text) when an option's sublabel or photo query shows
    something an earlier round already showed; the exact-string check missed reworded repeats."""
    earlier = [
        (x.question.index, text)
        for x in snap.answers
        for text in (x.question.option_a.sublabel, x.question.option_b.sublabel, *x.image_queries)
        if text
    ]
    for opt in (draft.option_a, draft.option_b):
        for text in (opt.sublabel, opt.image_query):
            for index, before in earlier:
                if shared := _shared_subject(text, before):
                    return shared, index, before
    return None


_REF_FILLER = {
    "the", "and", "of", "de", "do", "da", "del", "la", "el", "le", "les", "at", "in", "on", "an", "a", "with",
    "local", "culture", "scene", "style", "food", "street", "shop", "store", "market", "city", "town", "area",
}


def _ref_in_options(draft: GenQuestion) -> bool:
    """Does ``local_reference`` name something actually on the cards (label, sublabel or image_query)?
    "Seongsu screen-print studio" on a heavyweight-cotton-vs-washed-jersey round doesn't."""
    ref_words = {w for w in _norm(draft.local_reference).split() if len(w) >= 3 and w not in _REF_FILLER}
    if not ref_words:
        return False
    shown = _norm(" ".join(
        f"{o.label} {o.sublabel or ''} {o.image_query}" for o in (draft.option_a, draft.option_b)
    )).split()
    return any(any(s.startswith(w[:5]) or w.startswith(s[:5]) for s in shown if len(s) >= 3) for w in ref_words)


def _shares_word(a: str | None, b: str | None) -> bool:
    """Do two phrases share a content word (same 5-letter stem)? "Flohmarkt am Mauerpark" and
    "Mauerpark flea-market wander" do; "The Getty Center" and "Reading nook" don't."""
    words_a = [w for w in _norm(a).split() if len(w) >= 4 and w not in _REF_FILLER]
    words_b = [w for w in _norm(b).split() if len(w) >= 4 and w not in _REF_FILLER]
    return any(x[:5] == y[:5] for x in words_a for y in words_b)


def _pasted_sublabel(option: GenOption, local_reference: str | None) -> bool:
    """A Latin-script sublabel that is just the round's local_reference and has nothing to do with
    the label: the model pasting the reference in to satisfy the "on the cards" rule (live games:
    "The Getty Center" under "Perfectly practical reading nook"). Native-script sublabels are left
    alone: "응원봉" under "Blinding concert lightstick" is the thing's real name."""
    sub = option.sublabel
    return bool(
        sub and local_reference and _norm(sub) == _norm(local_reference)
        and not _has_native_script(sub) and not _shares_word(sub, option.label)
    )


def _without_pasted_sublabels(draft: GenQuestion) -> GenQuestion:
    """``draft`` minus duplicated or pasted-in sublabels (see ``_pasted_sublabel``)."""
    a, b = draft.option_a, draft.option_b
    same = bool(_norm(a.sublabel)) and _norm(a.sublabel) == _norm(b.sublabel)

    def fix(o: GenOption) -> GenOption:
        if same or _pasted_sublabel(o, draft.local_reference):
            return o.model_copy(update={"sublabel": None, "sublabel_en": None})
        return o

    return draft.model_copy(update={"option_a": fix(a), "option_b": fix(b)})


_COUNTRY_FILLER = {
    "the", "of", "and", "south", "north", "east", "west", "central", "republic", "united", "kingdom", "states",
    "democratic", "people", "peoples", "islands", "island", "saint", "new", "federal", "arab", "emirates",
}
_DEMONYM_ENDINGS = ("n", "ese", "ish", "i", "ic", "ine")
# Names and demonyms the generic rule can't derive from the ISO country name.
_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "US": ("america", "american", "americans", "usa"), "GB": ("britain", "british", "brits"),
    "FR": ("french",), "NL": ("dutch", "holland"), "CH": ("swiss",), "PH": ("filipino", "filipina", "pinoy"),
    "TH": ("thai",), "CN": ("chinese",), "TW": ("taiwanese",), "VN": ("vietnamese",), "ES": ("spanish",),
    "SE": ("swedish",), "DK": ("danish",), "FI": ("finnish",), "IE": ("irish",), "GR": ("greek",),
    "TR": ("turkish",), "PT": ("portuguese",), "AE": ("emirati",), "NZ": ("kiwis",), "KR": ("korean",),
}


def _country_names(country: Country) -> tuple[str, ...]:
    """Name words and known demonyms of ``country`` ("korea", "korean"); images.find_image also
    matches demonyms derived from the name words ("mexico" -> "mexican")."""
    names = [n for n in _norm(country.name).split() if n not in _COUNTRY_FILLER and len(n) >= 4]
    return tuple(dict.fromkeys([*names, *_COUNTRY_ALIASES.get(country.code, ())]))


def _is_country_word(word: str, country: Country) -> bool:
    w = _norm(word)
    names = [n for n in _norm(country.name).split() if n not in _COUNTRY_FILLER and len(n) >= 4]
    if w in _COUNTRY_ALIASES.get(country.code, ()):
        return True
    return any(
        w == name or (len(w) >= 4 and w.startswith(name[:4]) and w.endswith(_DEMONYM_ENDINGS)) for name in names
    )


def _country_word_positions(words: list[str], country: Country) -> list[int]:
    """Indexes of words naming the country or nationality, except inside a multi-word proper noun:
    a country word followed by a capitalized word ("Mexico City", "Indian Railways", "Japan Rail
    Pass") belongs to that name and is kept."""
    positions = []
    for i, word in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if _is_country_word(word, country) and not nxt[:1].isupper():
            positions.append(i)
    return positions


def _country_words(text: str, country: Country) -> list[str]:
    """Capitalized country / nationality words used on their own ("Brazilian edition", "through
    America"); lowercase look-alikes ("fine china", "nail polish") don't count."""
    words = text.split()
    return [_norm(words[i]) for i in _country_word_positions(words, country) if words[i][:1].isupper()]


_DETERMINERS = {"a", "an", "the", "one", "your", "my", "our", "this", "that", "every", "some", "any", "its"}


def _drop_nationality(text: str, country: Country) -> str:
    """Remove a nationality adjective where that is always grammatical: right after a determiner
    or at the start, before a lowercase noun ("one Brazilian centerpiece" -> "one centerpiece",
    "Brazilian pop icon sticker" -> "Pop icon sticker"). Proper names ("Mexico City") stay, and so
    do phrasings where removal would break the sentence ("Brazilian edition" after a comma); the
    soft check asks the model to rephrase those."""
    words = text.split()
    drop = {
        i for i in _country_word_positions(words, country)
        if words[i][:1].isupper()
        and i + 1 < len(words) and words[i + 1][:1].islower()
        and (i == 0 or words[i - 1].lower() in _DETERMINERS)
    }
    if not drop or len(drop) == len(words):
        return text
    kept = [w for i, w in enumerate(words) if i not in drop]
    if 0 in drop:
        kept[0] = kept[0][:1].upper() + kept[0][1:]
    return " ".join(kept)


# Countries whose everyday language is English: a sublabel there can only restate the label in
# other words (live US games: "Halal cart on a Manhattan corner" / "Street meat"), so it is dropped.
_ENGLISH_ONLY = {"US", "GB", "AU", "NZ", "IE"}

# Scripts locals write their own names in, for countries that don't (only) use Latin letters. Any
# other country gets Latin-script sublabels only: asked for a "native-script" name, the model wrote
# a Lagos market in Arabic letters and "puff-puff" half in Cyrillic.
_CJK = {"CJK", "HIRAGANA", "KATAKANA", "HALFWIDTH", "FULLWIDTH", "IDEOGRAPHIC"}
_ARABIC_SCRIPT = ("SA", "AE", "EG", "MA", "DZ", "TN", "LY", "SD", "JO", "LB", "SY", "IQ", "KW", "QA", "BH", "OM", "YE",
                  "PS", "IR", "AF", "PK")
_CYRILLIC_SCRIPT = ("RU", "UA", "BY", "BG", "RS", "MK", "ME", "BA", "KZ", "KG", "TJ", "MN")
_COUNTRY_SCRIPTS: dict[str, set[str]] = {
    "KR": {"HANGUL"}, "JP": _CJK, "CN": _CJK, "TW": _CJK, "HK": _CJK, "MO": _CJK, "SG": _CJK | {"TAMIL"},
    "TH": {"THAI"}, "LA": {"LAO"}, "KH": {"KHMER"}, "MM": {"MYANMAR"}, "IL": {"HEBREW"}, "GR": {"GREEK"},
    "CY": {"GREEK"}, "GE": {"GEORGIAN"}, "AM": {"ARMENIAN"}, "ET": {"ETHIOPIC"}, "ER": {"ETHIOPIC"},
    "IN": {"DEVANAGARI", "TAMIL", "BENGALI", "TELUGU", "KANNADA", "MALAYALAM", "GUJARATI", "GURMUKHI", "ORIYA"},
    "NP": {"DEVANAGARI"}, "BD": {"BENGALI"}, "LK": {"SINHALA", "TAMIL"}, "MY": _CJK | {"TAMIL", "ARABIC"},
    **{code: {"ARABIC"} for code in _ARABIC_SCRIPT}, **{code: {"CYRILLIC"} for code in _CYRILLIC_SCRIPT},
}


def _letter_scripts(text: str) -> set[str]:
    return {unicodedata.name(ch, "?").split(" ")[0].split("-")[0] for ch in text if ch.isalpha()} - {"?"}


def _foreign_script(text: str | None, country: Country) -> bool:
    """Is ``text`` written (partly) in a script locals don't use ("سوق بالوغون" or "Пuff-puff" in
    Nigeria)? Latin letters are always fine ("PC방", "Jリーグ")."""
    allowed = _COUNTRY_SCRIPTS.get(country.code, set()) | {"LATIN"}
    return bool(text) and bool(_letter_scripts(text) - allowed)


def _localize(draft: GenQuestion, country: Country) -> GenQuestion:
    """Deterministic cleanup for the prompt and labels (works even when a soft-issue retry was
    skipped because the player was already waiting): nationality words, and Latin-script sublabels
    in countries whose language is English."""
    english = country.code in _ENGLISH_ONLY

    def opt(o: GenOption) -> GenOption:
        update: dict[str, Any] = {"label": _drop_nationality(o.label, country)}
        if (english and o.sublabel and not _has_native_script(o.sublabel)) or _foreign_script(o.sublabel, country):
            update |= {"sublabel": None, "sublabel_en": None}
        return o.model_copy(update=update)

    return draft.model_copy(update={
        "prompt": _drop_nationality(draft.prompt, country),
        "option_a": opt(draft.option_a),
        "option_b": opt(draft.option_b),
    })


def _image_query(option: GenOption, country: Country) -> str:
    """Drop the player's own country name / nationality from a photo query: the subject is already
    local, and the extra AND-ed word mostly makes searches come back empty ("Brazilian embroidery
    hoop" -> "embroidery hoop"). Multi-word proper nouns stay whole ("Mexico City taqueria",
    "Indian Railways train"): without "Mexico", "City taqueria" finds Lincoln City, Oregon. Keeps
    the query if nothing else would remain."""
    words = option.image_query.split()
    drop = set(_country_word_positions(words, country))
    kept = [w for i, w in enumerate(words) if i not in drop]
    return " ".join(kept) if kept else option.image_query


def _truncate_words(text: str, limit: int) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit]).rstrip(",;:") + "…"


def _tidy(draft: GenQuestion) -> GenQuestion:
    """Normalize whitespace and apply safe auto-fixes after validation."""

    def opt(o: GenOption) -> GenOption:
        label = _clean(o.label)
        sublabel = _clean(o.sublabel) or None
        if sublabel and _norm(sublabel) == _norm(label):
            sublabel = None
        query = " ".join(_clean(o.image_query).split()[: LIMITS["query_words"]]) or label
        wiki_lang = _clean(o.wiki_lang).lower()[:12] or None
        wiki_title = _clean(o.wiki_title)[:200] or None
        return GenOption(
            label=label, sublabel=sublabel, sublabel_en=(_clean(o.sublabel_en)[:120] or None) if sublabel else None,
            emoji=_clean(o.emoji)[:16] or "✨", color=o.color, image_query=query,
            wiki_lang=wiki_lang if wiki_title else None, wiki_title=wiki_title if wiki_lang else None,
        )

    # Identical sublabels tell the two cards apart by nothing, and a pasted-in local_reference
    # names something that isn't on the card: both are dropped (the label carries the option).
    return _without_pasted_sublabels(GenQuestion(
        hypothesis=_truncate_words(_clean(draft.hypothesis), HYPOTHESIS_MAX_WORDS),
        dimension=_clean(draft.dimension)[:80] or "general taste",
        why=_clean(draft.why)[:300],
        local_reference=_clean(draft.local_reference)[:120] or None,
        twist=draft.twist,
        prompt=_frame_only(_clean(draft.prompt)),
        option_a=opt(draft.option_a),
        option_b=opt(draft.option_b),
    ))


def _frame_only(prompt: str) -> str:
    """A prompt that still asks "X or Y?" after the retry becomes its frame: "On the final tee:
    keep it close or let it speak?" -> "On the final tee:" (no frame -> "Pick one:")."""
    if not re.search(r"\bor\b", _norm(prompt)):
        return prompt
    head, colon, _ = prompt.partition(":")
    frame = head.strip()
    return f"{frame}:" if colon and frame and not re.search(r"\bor\b", _norm(frame)) else "Pick one:"


# --------------------------------------------------------------------------- generation


_effort = {"value": REASONING_EFFORT}


def _rejects_effort(err: LLMError) -> bool:
    """True if the provider refused the reasoning_effort value (not a transient failure)."""
    cause = err.__cause__
    return isinstance(cause, openai.BadRequestError) and "reasoning" in str(cause).lower()


async def _ask_once(user: str, system: str) -> GenQuestion:
    """One generation attempt (``chat_json`` adds its own JSON-repair retry).

    The nonce stops gateway response caches (Portkey caches exact repeats) from replaying another
    player's question for an identical prompt, and makes hedged attempts independent.
    """
    user = f"{user}\n(request {secrets.token_hex(4)})"
    effort = _effort["value"]
    for attempt_effort in (effort, FALLBACK_EFFORT):
        try:
            async with asyncio.timeout(GENERATION_TIMEOUT_S):
                return await llm.chat_json(
                    get_settings().question_model,
                    system,
                    user,
                    GenQuestion,
                    max_tokens=QUESTION_MAX_TOKENS,
                    temperature=0.9,
                    reasoning_effort=attempt_effort,
                )
        except LLMError as err:
            if attempt_effort == FALLBACK_EFFORT or not _rejects_effort(err):
                raise
            log.warning("question model rejected reasoning_effort=%r; using %r from now on", effort, FALLBACK_EFFORT)
            _effort["value"] = FALLBACK_EFFORT
    raise LLMError()  # pragma: no cover - loop always returns or raises


async def _ask(user: str, system: str) -> GenQuestion:
    """Generation with a hedge against tail latency.

    The gateway occasionally stalls for 30-70 s (or times out) on a request that normally takes
    ~4 s. If the first attempt fails, or hasn't answered after HEDGE_AFTER_S, a second attempt
    starts; the first success wins and the other is cancelled. Raises LLMError if both fail.
    """
    attempts = {asyncio.ensure_future(_ask_once(user, system))}
    hedged = False
    last_error: BaseException | None = None
    try:
        while attempts:
            done, attempts = await asyncio.wait(
                attempts, timeout=None if hedged else HEDGE_AFTER_S, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task.exception() is None:
                    return task.result()
                last_error = task.exception()
            if not hedged:
                hedged = True
                log.info("question attempt %s; starting a hedge", "failed" if done else "is slow")
                attempts.add(asyncio.ensure_future(_ask_once(user, system)))
    finally:
        for task in attempts:
            task.cancel()
    if isinstance(last_error, LLMError):
        raise last_error
    raise LLMError("The next question took too long. Please try again.") from last_error


async def _soft_retry_or_keep(retry: asyncio.Future[GenQuestion], wanted: asyncio.Event | None) -> bool:
    """Wait for a soft-only corrective retry unless the player starts waiting for this round first.

    Returns True if the retry finished (its result/exception is ready), False if ``wanted`` fired
    first (the retry is then cancelled and the first draft is served at once).
    """
    if wanted is None:
        await asyncio.wait({retry})
        return True
    waiter = asyncio.ensure_future(wanted.wait())
    try:
        await asyncio.wait({retry, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    if retry.done():
        return True
    retry.cancel()
    return False


async def _draft_question(snap: _Snapshot, must_local: bool, wanted: asyncio.Event | None = None) -> GenQuestion:
    """LLM draft + validation, with one corrective retry. Raises LLMError if nothing usable.

    Hard problems always get the retry. Soft-only problems get it only while nobody is waiting:
    ``wanted`` is set once a player needs this round (an answer arrived for a prefetch, or a fresh
    generation), and from then on the usable first draft is served instead of paying 3-7 s more.
    """
    user = _build_user_prompt(snap, must_local)
    system = _system_prompt(snap.country)
    first = await _ask(user, system)
    hard, soft = _problems(first, snap, must_local)
    if not hard and not soft:
        return first
    issues = "\n".join(f"- {p}" for p in hard + soft)
    if not hard and wanted is not None and wanted.is_set():
        log.info("round %d: keeping a draft with soft issues (player is waiting): %s", snap.next_index, "; ".join(soft))
        return first

    log.info("round %d draft rejected (%d hard, %d soft): %s", snap.next_index, len(hard), len(soft), "; ".join(hard + soft))
    retry_user = (
        f"{user}\n\nYour previous draft broke these rules:\n{issues}\n"
        f"Previous draft: {first.model_dump_json()}\n"
        "Write a corrected version of this round that fixes every problem (you may change the whole question)."
    )
    retry = asyncio.ensure_future(_ask(retry_user, system))
    try:
        if not hard and not await _soft_retry_or_keep(retry, wanted):
            log.info("round %d: player is waiting; serving the first draft", snap.next_index)
            return first
        second = await retry
    except LLMError:
        if hard:
            raise LLMError("We couldn't come up with a good question. Please try again.") from None
        return first  # only soft issues: the first draft is still usable
    finally:
        if not retry.done():
            retry.cancel()
    hard2, soft2 = _problems(second, snap, must_local)
    # Fewest hard, then fewest soft problems; ties go to the corrected draft (listed first).
    best, best_hard = min(((second, hard2, soft2), (first, hard, soft)), key=lambda t: (len(t[1]), len(t[2])))[:2]
    if best_hard:
        raise LLMError("We couldn't come up with a good question. Please try again.")
    return best


def _has_native_script(text: str) -> bool:
    """True for Hangul, kana, CJK, Devanagari, Thai, Arabic, Cyrillic... (letters outside Latin)."""
    return any(ch.isalpha() and not ch.isascii() and "LATIN" not in unicodedata.name(ch, "") for ch in text)


def _photo_spec(option: GenOption, country: Country) -> images.PhotoSpec:
    """What a card's photo should show: its label, photo query, native name and Wikipedia article."""
    return images.PhotoSpec(
        label=option.label,
        query=_image_query(option, country),
        sublabel=option.sublabel,
        wiki_lang=option.wiki_lang,
        wiki_title=option.wiki_title,
        prefer=_photo_prefer(option, country),
    )


async def _option_images(
    a: GenOption,
    b: GenOption,
    country: Country,
    shown: frozenset[str] = frozenset(),
    wanted: asyncio.Event | None = None,
    prompt: str = "",
) -> tuple[OptionImage | None, OptionImage | None]:
    """Both cards' photos (one ``images.find_pair`` call: Wikipedia lead images, Commons, one judge
    call), never repeating one already ``shown`` in this game. Cards must match: a photo next to a
    plain gradient tilts the choice, so it is both or neither. Whatever isn't ready within
    IMAGE_BUDGET_S becomes the gradient + emoji fallback; once a player waits for the round
    (``wanted``), a card without a photo gets no extra tier.
    """
    context = f"Player's country: {_country_label(country)}. Question: \"{prompt}\""
    try:
        async with asyncio.timeout(IMAGE_BUDGET_S):
            img_a, img_b = await images.find_pair(
                _photo_spec(a, country), _photo_spec(b, country), exclude=shown, context=context, hurry=wanted
            )
    except TimeoutError:
        log.info("quiz: photos took over %.0fs; using the gradient fallback on both", IMAGE_BUDGET_S)
        return None, None
    except asyncio.CancelledError:
        raise
    except Exception:  # images.find_pair shouldn't raise, but a picture is never worth a failed round
        log.exception("photo lookup failed for a quiz round")
        return None, None
    if img_a is None or img_b is None or img_a.url == img_b.url:
        return None, None
    return img_a, img_b


def _photo_prefer(option: GenOption, country: Country) -> tuple[str, ...]:
    """``images.find_image(prefer=...)`` for a card: the country's names, plus the card's native-script
    sublabel (Commons describes many local photos only in the local script: "여의도동, 서울")."""
    names = _country_names(country)
    return (*names, option.sublabel) if option.sublabel and _has_native_script(option.sublabel) else names


def _shown_images(snap: _Snapshot) -> frozenset[str]:
    return frozenset(
        o.image.url for a in snap.answers for o in (a.question.option_a, a.question.option_b) if o.image is not None
    )


async def _generate(snap: _Snapshot, wanted: asyncio.Event | None = None) -> _Generated:
    """Produce the next round for ``snap`` (pure: never touches the session store).

    ``wanted`` (see ``_draft_question``): set when a player is waiting for this round.
    """
    started = time.perf_counter()
    index = snap.next_index
    local_so_far = sum(1 for a in snap.answers if snap.local_refs.get(a.question.id))
    must_local = _local_required(index, local_so_far)
    draft = _drop_mismatched_sublabels(_localize(_tidy(await _draft_question(snap, must_local, wanted)), snap.country))
    if _forever_used_up(snap):  # backstop when the soft retry was skipped (player waiting) or ignored
        draft = draft.model_copy(update={"prompt": _drop_forever(draft.prompt)})
    llm_done = time.perf_counter()
    img_a, img_b = await _option_images(
        draft.option_a, draft.option_b, snap.country, _shown_images(snap), wanted, draft.prompt
    )

    def option(o: GenOption, image: OptionImage | None) -> Option:
        return Option(label=o.label, sublabel=o.sublabel, emoji=o.emoji, color=o.color, image=image)

    question = Question(
        id=store.new_id(),
        index=index,
        total=QUIZ_TOTAL,
        prompt=draft.prompt,
        depth=depth_for_round(index),
        option_a=option(draft.option_a, img_a),
        option_b=option(draft.option_b, img_b),
    )
    log.info(
        "quiz round %d ready in %.1fs (llm %.1fs, images %.1fs; dimension: %s)",
        index, time.perf_counter() - started, llm_done - started, time.perf_counter() - llm_done, draft.dimension,
    )
    # Only a reference that is actually on the cards counts toward the 2/3-local quota.
    local_ref = draft.local_reference if draft.local_reference and _ref_in_options(draft) else ""
    return _Generated(
        stored=StoredQuestion(
            question=question, dimension=draft.dimension, rationale=draft.why or None,
            image_queries=[draft.option_a.image_query, draft.option_b.image_query],
        ),
        hypothesis=draft.hypothesis or snap.hypothesis,
        local_ref=local_ref,
    )


# --------------------------------------------------------------------------- task registries


@dataclass
class _Prefetch:
    task: asyncio.Task[_Generated]
    created: float
    wanted: asyncio.Event = field(default_factory=asyncio.Event)  # set once a player waits for it


# (session_id, question_id, choice) -> speculative generation of the following round
_prefetch: dict[tuple[str, str, str], _Prefetch] = {}
# "session_id:answers_count" -> in-flight generation for start_quiz (dedupes double starts)
_starts: dict[str, asyncio.Task[_Generated]] = {}
# country code -> round 1 generated ahead of time (POST /quiz/warm); consumed by ONE session
_warm: dict[str, _Prefetch] = {}
# fire-and-forget work (final hypothesis); strong refs so tasks aren't garbage collected
_background: set[asyncio.Task[Any]] = set()


def _waiting() -> asyncio.Event:
    """An already-set ``wanted`` event: a player is waiting for this generation right now."""
    event = asyncio.Event()
    event.set()
    return event


def _retrieve_exception(task: asyncio.Task[Any]) -> None:
    """Done-callback: consume failures of tasks nobody awaited (avoids 'never retrieved' noise)."""
    if not task.cancelled() and (err := task.exception()) is not None:
        log.info("background quiz task %s failed: %s", task.get_name(), type(err).__name__)


def _on_this_loop(task: asyncio.Task[Any]) -> bool:
    try:
        return task.get_loop() is asyncio.get_running_loop()
    except RuntimeError:
        return False


def _cancel(task: asyncio.Task[Any]) -> None:
    if not task.done() and _on_this_loop(task):
        task.cancel()


def _evict_stale() -> None:
    """Drop entries past their TTL, from dead event loops, or beyond the size cap."""
    now = time.monotonic()
    for key, entry in list(_prefetch.items()):
        if now - entry.created > PREFETCH_TTL_S or not _on_this_loop(entry.task):
            _cancel(entry.task)
            del _prefetch[key]
    overflow = len(_prefetch) - PREFETCH_MAX
    if overflow > 0:
        for key, entry in sorted(_prefetch.items(), key=lambda kv: kv[1].created)[:overflow]:
            _cancel(entry.task)
            del _prefetch[key]


def _drop_prefetch(session_id: str, question_id: str | None = None, choice: str | None = None) -> None:
    """Cancel and forget prefetches of a session (optionally one question / one branch)."""
    for key in [k for k in _prefetch if k[0] == session_id]:
        if (question_id is None or key[1] == question_id) and (choice is None or key[2] == choice):
            _cancel(_prefetch.pop(key).task)


def _ensure_prefetch(rec: SessionRecord) -> None:
    """Start speculative generation of the next round for both choices, unless already running/done."""
    current = rec.current
    if rec.quiz_done or current is None or current.question.index >= QUIZ_TOTAL:
        return
    _evict_stale()
    for choice in ("A", "B"):
        key = (rec.id, current.question.id, choice)
        entry = _prefetch.get(key)
        if entry and _on_this_loop(entry.task):
            failed = entry.task.done() and (entry.task.cancelled() or entry.task.exception() is not None)
            if not failed:
                continue
        wanted = asyncio.Event()
        task = asyncio.create_task(
            _generate(_Snapshot.of(rec, assume=choice), wanted),
            name=f"quiz-prefetch:{rec.id}:{current.question.index + 1}{choice}",
        )
        task.add_done_callback(_retrieve_exception)
        _prefetch[key] = _Prefetch(task, time.monotonic(), wanted)


async def _take_prefetch(session_id: str, question_id: str, choice: str) -> _Generated | None:
    """Await the matching speculative result; None if absent, from another loop, cancelled or failed."""
    entry = _prefetch.get((session_id, question_id, choice))
    if entry is None or not _on_this_loop(entry.task):
        return None
    entry.wanted.set()  # the player is waiting now: no more optional (soft-issue) retries
    try:
        # shield: if this request is cancelled, the shared prefetch keeps running for a retry
        return await asyncio.shield(entry.task)
    except asyncio.CancelledError:
        if entry.task.cancelled():
            return None
        raise
    except Exception as err:
        log.info("prefetch for %s/%s failed (%s); generating fresh", session_id, choice, type(err).__name__)
        return None


def warm(country: Country) -> None:
    """Start generating a round 1 for ``country`` ahead of time (fire-and-forget).

    The landing page calls this when a country is picked, so the first question is ready by the
    time the game starts (round 1 needs no player input). At most one warm round per country is
    kept, for WARM_TTL_S; ``start_quiz`` hands it to ONE session (question ids are never shared).
    """
    _evict_warm()
    entry = _warm.get(country.code)
    if entry is not None and _on_this_loop(entry.task) and not _failed(entry.task):
        return  # one is ready or on its way
    snap = _Snapshot(session_id=f"warm-{secrets.token_hex(6)}", country=country.model_copy(), answers=(), hypothesis="")
    wanted = asyncio.Event()
    task = asyncio.create_task(_generate(snap, wanted), name=f"quiz-warm:{country.code}")
    task.add_done_callback(_retrieve_exception)
    _warm[country.code] = _Prefetch(task, time.monotonic(), wanted)


def _failed(task: asyncio.Task[Any]) -> bool:
    return task.done() and (task.cancelled() or task.exception() is not None)


def _evict_warm() -> None:
    now = time.monotonic()
    for code, entry in list(_warm.items()):
        if now - entry.created > WARM_TTL_S or not _on_this_loop(entry.task) or _failed(entry.task):
            _cancel(entry.task)
            del _warm[code]
    for code, entry in sorted(_warm.items(), key=lambda kv: kv[1].created)[: max(0, len(_warm) - WARM_MAX)]:
        _cancel(entry.task)
        del _warm[code]


async def _take_warm(country_code: str) -> _Generated | None:
    """Claim the warm round 1 for this country (it leaves the registry), or None."""
    _evict_warm()
    entry = _warm.pop(country_code, None)
    if entry is None:
        return None
    entry.wanted.set()
    try:
        return await asyncio.shield(entry.task)
    except asyncio.CancelledError:
        if entry.task.cancelled():
            return None
        raise
    except Exception as err:
        log.info("warm round 1 for %s failed (%s); generating fresh", country_code, type(err).__name__)
        return None


async def _first_round(rec: SessionRecord) -> _Generated:
    """The round after ``rec.answers``: a warm round 1 if one is ready, else a fresh generation."""
    if not rec.answers:
        warmed = await _take_warm(rec.country.code)
        if warmed is not None:
            log.info("quiz: session %s starts with a warm round 1", rec.id)
            return warmed
    return await _generate(_Snapshot.of(rec), _waiting())


async def _shared_start(rec: SessionRecord) -> _Generated:
    """Generate the round after ``rec.answers``; concurrent starts for the same session share one task."""
    key = f"{rec.id}:{len(rec.answers)}"
    task = _starts.get(key)
    if task is None or not _on_this_loop(task):
        task = asyncio.create_task(_first_round(rec), name=f"quiz-start:{rec.id}")
        task.add_done_callback(_retrieve_exception)
        task.add_done_callback(lambda t, k=key: _starts.pop(k, None) if _starts.get(k) is t else None)
        _starts[key] = task
    return await asyncio.shield(task)


def _spawn_background(coro: Coroutine[Any, Any, Any], name: str) -> None:
    task = asyncio.create_task(coro, name=name)
    _background.add(task)
    task.add_done_callback(_background.discard)
    task.add_done_callback(_retrieve_exception)


def prefetch_keys() -> list[tuple[str, str, str]]:
    """Keys of live prefetch entries (debugging / tests)."""
    return list(_prefetch)


async def shutdown() -> None:
    """Cancel every prefetch / start / warm / background task on this loop and wait for them."""
    tasks = (
        [e.task for e in _prefetch.values()] + list(_starts.values()) + [e.task for e in _warm.values()]
        + list(_background)
    )
    _prefetch.clear()
    _starts.clear()
    _warm.clear()
    _recent_refs.clear()
    mine = [t for t in tasks if _on_this_loop(t)]
    for t in mine:
        t.cancel()
    if mine:
        await asyncio.gather(*mine, return_exceptions=True)
    _background.clear()


async def forget_session(session_id: str) -> None:
    """Cancel a session's speculative work (DELETE /api/session/{id})."""
    _drop_prefetch(session_id)
    for key in [k for k in _starts if k.startswith(f"{session_id}:")]:
        _cancel(_starts.pop(key))


# --------------------------------------------------------------------------- session flows


def _commit(rec: SessionRecord, gen: _Generated) -> None:
    rec.current = gen.stored
    if gen.hypothesis:
        rec.hypothesis = gen.hypothesis
    refs = rec.extra.get(LOCAL_REFS_KEY)
    if not isinstance(refs, dict):
        refs = rec.extra[LOCAL_REFS_KEY] = {}
    refs[gen.stored.question.id] = gen.local_ref
    _remember_ref(rec.country.code, gen.local_ref)


def _require_current(rec: SessionRecord, answer: Answer) -> StoredQuestion:
    if rec.quiz_done:
        raise ConflictError("You've already finished the game.")
    if rec.current is None:
        raise ConflictError("The game hasn't started yet.")
    if rec.current.question.id != answer.question_id:
        raise ConflictError("That question is no longer current. Reload to continue.")
    return rec.current


def _replay(rec: SessionRecord, answer: Answer) -> AnswerResult | None:
    """Idempotency: the same answer arriving twice (double tap, retry) gets the same result."""
    last = rec.answers[-1] if rec.answers else None
    if last and last.question.id == answer.question_id and last.choice == answer.choice:
        return AnswerResult(next=rec.current.question if rec.current else None, done=rec.quiz_done)
    return None


async def start_quiz(session_id: str) -> Question:
    """Return the current question, generating round 1 if the quiz hasn't started.

    Idempotent: if ``rec.current`` is set, return it unchanged (and make sure its prefetch tasks
    are running). Persists the new question as ``rec.current = StoredQuestion(...)``.

    Raises:
        NotFoundError: unknown session.
        ConflictError: quiz already finished.
        LLMError: generation failed.
    """
    rec = store.require_session(session_id)
    if rec.quiz_done:
        raise ConflictError("You've already finished the game.")
    if rec.current is None:
        generated = await _shared_start(rec)
        async with store.edit_session(session_id) as rec:
            if rec.quiz_done:
                raise ConflictError("You've already finished the game.")
            if rec.current is None and generated.stored.question.index == len(rec.answers) + 1:
                _commit(rec, generated)
            if rec.current is None:  # state moved on unexpectedly; let the client retry
                raise ConflictError("The game changed in another tab. Reload to continue.")
    _ensure_prefetch(rec)
    return rec.current.question


async def answer(session_id: str, answer: Answer) -> AnswerResult:
    """Record ``answer`` for the current question and return the next one.

    Round QUIZ_TOTAL -> ``AnswerResult(next=None, done=True)``. On any failure the session is left
    unchanged so the client can resend the same ``Answer``. A duplicate of the last accepted answer
    returns the same result again.

    Raises:
        NotFoundError: unknown session.
        ConflictError: stale/duplicate question_id with a different choice, quiz not started, or done.
        LLMError: the next question could not be generated.
    """
    rec = store.require_session(session_id)
    if replay := _replay(rec, answer):
        return replay
    current = _require_current(rec, answer)
    other = "B" if answer.choice == "A" else "A"
    _drop_prefetch(session_id, current.question.id, other)  # the losing branch is never needed

    if current.question.index >= QUIZ_TOTAL:
        async with store.edit_session(session_id) as rec:
            if replay := _replay(rec, answer):
                return replay
            current = _require_current(rec, answer)
            rec.answers.append(_answered(current, answer.choice))
            rec.current = None
            rec.quiz_done = True
            final = _Snapshot.of(rec)
        _drop_prefetch(session_id)
        _spawn_background(_finalize_hypothesis(final), name=f"quiz-final-hypothesis:{session_id}")
        return AnswerResult(next=None, done=True)

    generated = await _take_prefetch(session_id, current.question.id, answer.choice)
    if generated is None:
        generated = await _generate(_Snapshot.of(rec, assume=answer.choice), _waiting())

    async with store.edit_session(session_id) as rec:
        if replay := _replay(rec, answer):
            return replay
        current = _require_current(rec, answer)
        rec.answers.append(_answered(current, answer.choice))
        _commit(rec, generated)
    _drop_prefetch(session_id, current.question.id)
    _ensure_prefetch(rec)
    return AnswerResult(next=generated.stored.question, done=False)


async def _finalize_hypothesis(snap: _Snapshot) -> None:
    """Fold the last pick into the hidden hypothesis (best effort, after the game is done)."""
    user = "\n".join(
        [
            f"Country: {_country_label(snap.country)}",
            f"Running hypothesis (before the final pick): {snap.hypothesis or '(none)'}",
            "History (oldest first):",
            *_history_lines(snap),
            "",
            "Return the updated hypothesis.",
        ]
    )
    try:
        async with asyncio.timeout(GENERATION_TIMEOUT_S):
            result = await llm.chat_json(
                get_settings().question_model, HYPOTHESIS_SYSTEM, user, HypothesisUpdate,
                max_tokens=400, reasoning_effort="low",
            )
    except (LLMError, TimeoutError) as err:
        log.info("final hypothesis update skipped: %s", type(err).__name__)
        return
    hypothesis = _truncate_words(_clean(result.hypothesis), HYPOTHESIS_MAX_WORDS)
    if not hypothesis:
        return
    async with store.edit_session(snap.session_id) as rec:
        # Only apply to the game we summarized, and never clobber a newer value.
        if rec.quiz_done and len(rec.answers) == len(snap.answers) and rec.hypothesis == snap.hypothesis:
            rec.hypothesis = hypothesis
