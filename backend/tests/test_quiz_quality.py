"""Question-quality rules, soft-retry racing and warm round 1 in app/quiz.py. Fully offline."""

from __future__ import annotations

import asyncio

import pytest

from app import quiz, store
from app.models import Country
from tests.factories import seed_session
from tests.test_quiz import _quiz_cleanup, _snap, payload, round_of, scripted  # noqa: F401  (fixtures)

# --------------------------------------------------------------------------- pure rules


@pytest.mark.parametrize(
    ("text", "code", "name", "expected"),
    [
        ("Mexico City taqueria", "MX", "Mexico", "Mexico City taqueria"),
        ("Panama City skyline", "PA", "Panama", "Panama City skyline"),
        ("Indian Railways train", "IN", "India", "Indian Railways train"),
        ("Japan Rail Pass", "JP", "Japan", "Japan Rail Pass"),
        ("Korea Town sign", "KR", "South Korea", "Korea Town sign"),
        ("Singapore Botanic Gardens", "SG", "Singapore", "Singapore Botanic Gardens"),
        ("Brazilian embroidery hoop", "BR", "Brazil", "embroidery hoop"),
        ("street food Korea", "KR", "South Korea", "street food"),
    ],
)
def test_image_query_keeps_multi_word_proper_nouns(text, code, name, expected):
    option = quiz.GenOption(label="x", emoji="x", color="#000000", image_query=text)
    assert quiz._image_query(option, Country(code=code, name=name)) == expected


def _soft(snap=None, must_local=False, **overrides) -> list[str]:
    snap = snap or _snap()
    draft = quiz.GenQuestion.model_validate(payload(snap.next_index, **overrides))
    return quiz._problems(draft, snap, must_local=must_local)[1]


def _with_prompts(*prompts: str) -> quiz._Snapshot:
    rec = seed_session(answered=len(prompts), started=False)
    live = store.require_session(rec.id)
    for answer, prompt in zip(live.answers, prompts):
        answer.question.prompt = prompt
    store.save_session(live)
    return quiz._Snapshot.of(store.require_session(rec.id))


def test_problems_flags_a_third_shirt_prompt_only():
    one_tee = _with_prompts("Your tee gets one color:", "Friday night:")
    assert not any("mention the shirt" in p for p in _soft(one_tee, prompt="Your t-shirt, forever:"))
    two_tees = _with_prompts("Your tee gets one color:", "On your chest:")
    assert any("mention the shirt" in p for p in _soft(two_tees, prompt="Your t-shirt, forever:"))
    assert not any("mention the shirt" in p for p in _soft(two_tees, prompt="Your bedroom wall:"))
    assert "frame this one as a life or culture dilemma" in quiz._build_user_prompt(two_tees, must_local=False)


def test_forever_frames_are_capped_per_game_and_never_back_to_back():
    fresh = _with_prompts("Friday night:", "Saturday morning:")
    assert not any("'forever'" in p for p in _soft(fresh, prompt="Your desk, forever:"))
    assert "Don't use 'forever'" not in quiz._build_user_prompt(fresh, must_local=False)
    back_to_back = _with_prompts("Friday night:", "Lunch forever:")
    assert any("'forever'" in p for p in _soft(back_to_back, prompt="Your desk, forever:"))
    assert "Don't use 'forever'" in quiz._build_user_prompt(back_to_back, must_local=False)
    quota = _with_prompts("Pick one. Forever.", "Friday night:", "Your desk, forever:", "Sunday:", "Fabric for life:", "Hi:")
    assert any("'forever'" in p for p in _soft(quota, prompt="Snack wrapper, forever:"))
    assert not any("'forever'" in p for p in _soft(quota, prompt="Snack wrapper:"))
    twist = quiz._build_user_prompt(_with_prompts("Pick one. Forever.", "Lunch forever:"), must_local=False)
    assert "Twist round" in twist and "'forever'" not in twist.split("Twist round")[1]


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Your desk relic, forever:", "Your desk relic:"),
        ("Pick one. Forever.", "Pick one:"),
        ("Lunch forever:", "Lunch:"),
        ("Your forever game-day look:", "Your game-day look:"),
        ("Forever:", "Pick one:"),
        ("Forever stuck with:", "Forever stuck with:"),  # mid-sentence: left to the soft check
        ("Saturday night:", "Saturday night:"),
    ],
)
def test_drop_forever(prompt, expected):
    assert quiz._drop_forever(prompt) == expected


async def test_forever_past_the_quota_is_dropped_even_when_the_player_is_waiting(scripted, fake_images):  # noqa: F811
    snap = _with_prompts("Friday night:", "Lunch forever:")
    scripted.behavior = lambda user, schema, kw: payload(round_of(user), prompt="Your desk relic, forever:")
    generated = await quiz._generate(snap, quiz._waiting())
    assert len(scripted.calls) == 1 and generated.stored.question.prompt == "Your desk relic:"
    allowed = _with_prompts("Friday night:", "Saturday:")
    assert (await quiz._generate(allowed, quiz._waiting())).stored.question.prompt == "Your desk relic, forever:"


@pytest.mark.parametrize(
    ("ref", "a", "b", "expected"),
    [
        # Live games: the reference pasted under unrelated labels so it "appears on the cards".
        ("The Getty Center", ("Reading nook", "The Getty Center"), ("Statement lamp", "The Getty Center"), (None, None)),
        ("Nigerian compound house", ("Painted ceiling mural", "Nigerian compound house"), ("Reading loft", "mezzanine"),
         (None, "mezzanine")),
        ("K-pop lightstick", ("Blinding lightstick", "응원봉"), ("Soft glow lightstick", "응원봉"), (None, None)),
        # Real names stay: native script, or a Latin name that shares a word with its label.
        ("Flohmarkt am Mauerpark", ("Mauerpark flea-market wander", "Flohmarkt am Mauerpark"), ("Blanket reset", None),
         ("Flohmarkt am Mauerpark", None)),
        ("응원봉", ("Blinding lightstick", "응원봉"), ("Tour hoodie", "콘서트 후드티"), ("응원봉", "콘서트 후드티")),
    ],
)
def test_duplicated_or_pasted_sublabels_are_dropped(ref, a, b, expected):
    option_a = {"label": a[0], "sublabel": a[1], "image_query": a[0]}
    option_b = {"label": b[0], "sublabel": b[1], "image_query": b[0]}
    draft = quiz.GenQuestion.model_validate(payload(3, local_reference=ref, option_a=option_a, option_b=option_b))
    tidy = quiz._tidy(draft)
    assert (tidy.option_a.sublabel, tidy.option_b.sublabel) == expected
    # Fixed deterministically, never a retry trigger (retries delay prefetches; players wait for photos).
    assert not _soft(local_reference=ref, option_a=option_a, option_b=option_b)


async def test_a_pasted_reference_does_not_count_as_local(scripted, fake_images):  # noqa: F811
    scripted.behavior = lambda user, schema, kw: payload(
        round_of(user), local_reference="The Getty Center",
        option_a={"label": "Reading nook", "sublabel": "The Getty Center", "image_query": "reading nook chair"},
    )
    generated = await quiz._generate(_snap())
    assert generated.stored.question.option_a.sublabel is None
    assert generated.local_ref == ""  # so the 2/3-local quota asks a later round for a real local reference


def test_every_third_round_requires_a_named_twist():
    two = _with_prompts("Friday night:", "Saturday morning:")  # next is round 3
    twist = quiz._twist_for(two)
    assert twist in quiz.TWISTS
    prompt = quiz._build_user_prompt(two, must_local=False)
    assert "Twist round (required)" in prompt and f'Set twist to "{twist}"' in prompt
    assert "if it fits" not in prompt
    assert quiz._twist_for(_with_prompts("Friday night:")) is None  # round 2: plain
    assert "Twist round" not in quiz._build_user_prompt(_with_prompts("Friday night:"), must_local=False)
    # per-session order: different players get different twists for the same round
    picks = {quiz._twist_for(quiz._Snapshot(f"s{i}", two.country, two.answers, "")) for i in range(12)}
    assert len(picks) > 1
    # "forever" is never asked for once it is used up
    used = _with_prompts("Pick one. Forever.", "Lunch forever:")
    assert all(quiz._twist_for(quiz._Snapshot(f"s{i}", used.country, used.answers, "")) != "forever" for i in range(12))


def test_twist_round_without_a_twist_is_a_soft_issue():
    snap = _with_prompts("Friday night:", "Saturday morning:")
    assert any("this is a twist round" in p for p in _soft(snap, twist="none"))
    assert not any("twist round" in p for p in _soft(snap, twist="both_painful"))
    assert not any("twist round" in p for p in _soft(_with_prompts("Friday night:"), twist="none"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("perk_with_catch", "perk_with_catch"), ("Perk with a catch", "perk_with_catch"),
     ("Would-you-rather", "would_you_rather"), ("both options painful", "both_painful"), ("FOREVER", "forever"),
     ("", "none"), (None, "none"), ("plain", "none")],
)
def test_free_form_twist_answers_are_normalized(raw, expected):
    assert quiz.GenQuestion.model_validate(payload(3, twist=raw)).twist == expected


@pytest.mark.parametrize(
    ("dimension", "a", "b", "flagged"),
    [
        # live games: the label names the pole instead of showing a thing
        ("function vs beauty", "Perfectly useful storage wall", "Beautifully useless art object", ["B"]),
        ("function versus beauty", "The perfect toaster", "The beautiful fruit bowl", ["B"]),
        ("mainstream vs niche", "The chart-topping anthem", "Niche basement band", ["B"]),
        ("nostalgia vs novelty (old favorite vs brand-new)", "Nostalgic arcade cabinet", "Brand-new VR booth", ["A", "B"]),
        # fine: a category head isn't a pole, proper names don't count, things that embody a pole do
        ("social energy (big crowd vs small circle)", "Social club karaoke", "Suya with three friends", []),
        ("city buzz vs nature calm", "Mexico City taqueria", "Desert of the Lions trail", []),
        ("function vs beauty", "Muji drawer unit", "Tower of the Sun replica", []),
    ],
)
def test_problems_flags_labels_that_spell_out_the_trait(dimension, a, b, flagged):
    soft = _soft(dimension=dimension, option_a={"label": a}, option_b={"label": b})  # round 1 (broad)
    assert [side for side in "AB" if any(f"option {side} label spells out the trait" in p for p in soft)] == flagged


def test_specific_taste_rounds_may_name_the_look():
    """Round 11+: showing the look on a real thing is the point ("Neon Carnaval feathers")."""
    snap = _snap(answers=10)
    soft = _soft(snap, dimension="saturation (washed-out vs neon)",
                 option_a={"label": "Neon tteokbokki cart sign"}, option_b={"label": "Faded bathhouse tiles"})
    assert not any("spells out the trait" in p for p in soft)


@pytest.mark.parametrize(
    ("new", "old", "same"),
    [
        ("Máscara de lucha libre", "Lucha libre", True),
        ("Welcome to Fabulous Las Vegas", "Las Vegas Strip", True),
        ("school jacket emblem", "school emblem patch", True),
        ("떡볶이", "떡볶이", True),
        ("Route 66 vacancy sign", "Route 66", True),  # recheck live game: R10 then R14
        ("Studio 54 disco ball", "Route 66 diner", False),
        ("neon shop sign", "neon street sign", False),  # generic words don't make it the same thing
        ("tteokbokki 3", "tteokbokki 1", False),
        ("Seoul Metro map", "Seoul Forest park", False),
    ],
)
def test_same_thing_catches_reworded_repeats(new, old, same):
    assert quiz._same_thing(new, old) is same


def test_shared_subject_names_what_repeats():
    assert quiz._shared_subject("cartel de lucha libre", "Máscara de lucha libre") == "lucha libre"
    assert quiz._shared_subject("Welcome to Fabulous Las Vegas", "Las Vegas Strip") == "las vegas"
    assert quiz._shared_subject("school jacket emblem", "school emblem patch") == "school emblem"


def test_problems_flags_an_object_from_an_earlier_round_in_other_words():
    rec = seed_session(answered=2, started=False)
    live = store.require_session(rec.id)
    live.answers[1].image_queries = ["lucha libre mask", "monarch butterfly"]
    store.save_session(live)
    snap = quiz._Snapshot.of(store.require_session(rec.id))
    soft = _soft(snap, option_a={"sublabel": "Máscara de lucha libre", "image_query": "wrestling mask"})
    assert any("same thing as an earlier round: 'lucha libre' again (round 2" in p for p in soft)
    assert not any("same thing" in p for p in _soft(snap))  # the default round-3 draft is fresh


def test_problems_flags_sublabels_that_spell_out_the_trait():
    soft = _soft(
        dimension="social energy (big crowd vs small circle)",
        option_a={"sublabel": "Big crowd, bigger funnel cake"},
        option_b={"sublabel": "Small circle, lemonade in hand"},
    )
    assert sum("spells out the trait" in p for p in soft) == 2
    assert not _soft(dimension="social energy (big crowd vs small circle)")  # native names are fine


@pytest.mark.parametrize(
    ("code", "name", "prompt", "flagged"),
    [
        ("BR", "Brazil", "Saturday plans, Brazilian edition:", True),
        ("US", "United States", "Road trip through America:", True),
        ("MX", "Mexico", "Mexico City Sunday:", False),
        ("CN", "China", "Fine china, forever:", False),
        ("KR", "South Korea", "Friday night:", False),
    ],
)
def test_problems_flags_nationality_words(code, name, prompt, flagged):
    rec = seed_session(country=Country(code=code, name=name), started=False)
    soft = _soft(quiz._Snapshot.of(rec), prompt=prompt)
    assert any("country or nationality" in p for p in soft) is flagged


@pytest.mark.parametrize(
    ("text", "code", "name", "expected"),
    [
        ("Your living room gets one Brazilian centerpiece:", "BR", "Brazil", "Your living room gets one centerpiece:"),
        ("Brazilian pop icon sticker", "BR", "Brazil", "Pop icon sticker"),
        ("Your Korean comfort food:", "KR", "South Korea", "Your comfort food:"),
        ("Saturday plans, Brazilian edition:", "BR", "Brazil", "Saturday plans, Brazilian edition:"),  # left to the model
        ("Indian Coffee House with everyone", "IN", "India", "Indian Coffee House with everyone"),
        ("Mexico City taco crawl", "MX", "Mexico", "Mexico City taco crawl"),
    ],
)
def test_nationality_words_are_dropped_where_grammatical(text, code, name, expected):
    assert quiz._drop_nationality(text, Country(code=code, name=name)) == expected


def test_round_one_uses_the_players_first_shuffled_dimension():
    prompt = quiz._build_user_prompt(_snap(), must_local=True)
    first = quiz._dimension_order(_snap().session_id, "broad")[0]
    assert "Round 1 dimension:" in prompt
    rounds = {quiz._build_user_prompt(_snap(), must_local=True).split("Round 1 dimension: ")[1].split("\n")[0]
              for _ in range(6)}
    assert len(rounds) > 1 and first  # different players open with different axes


def test_problems_flags_repeated_objects_and_off_card_local_reference():
    snap = _snap(answers=2)  # factories: sublabels 떡볶이 / 호떡 in every earlier round
    assert any("same thing as an earlier round" in p for p in _soft(snap, option_a={"sublabel": "떡볶이"}))
    soft = _soft(must_local=True, local_reference="Seongsu screen-print studio")
    assert any("the options actually show" in p for p in soft)
    assert not _soft(must_local=True)  # "Tteokbokki alley 1" is on card A


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("On the final tee: keep it close or let it speak?", "On the final tee:"),
        ("Takes the whole front or stays sly?", "Pick one:"),
        ("Saturday night:", "Saturday night:"),
        ("Pick one. Forever.", "Pick one. Forever."),
    ],
)
def test_or_prompts_are_cut_to_their_frame(prompt, expected):
    assert quiz._tidy(quiz.GenQuestion.model_validate(payload(1, prompt=prompt))).prompt == expected


def test_system_prompt_leaves_out_the_players_own_country_examples():
    korea, japan = Country(code="KR", name="South Korea"), Country(code="JP", name="Japan")
    assert "짜장면" not in quiz._system_prompt(korea) and "純喫茶" in quiz._system_prompt(korea)
    assert "짜장면" in quiz._system_prompt(japan) and "純喫茶" not in quiz._system_prompt(japan)
    assert "your tee" not in quiz._system_prompt(japan).lower()  # no t-shirt-survey examples
    prompt = quiz._build_user_prompt(_snap(), must_local=True)  # a Korean player
    assert "jjajangmyeon vs jjamppong" in prompt.split("Local references already used")[1]


def test_dimension_ideas_are_shuffled_per_session():
    orders = {tuple(quiz._dimension_order(f"session-{i}", "broad")) for i in range(8)}
    assert len(orders) > 1
    assert quiz._dimension_order("same", "specific") == quiz._dimension_order("same", "specific")
    assert not any("fabric feel" in d or d.startswith("pace") for d in quiz._dimension_order("x", "broad"))


async def test_option_photos_prefer_the_players_country(scripted, fake_images):  # noqa: F811
    await quiz._generate(_snap())  # a South Korean player; factory sublabels are Hangul ("떡볶이 1")
    assert fake_images.prefers and all(p[:2] == ("korea", "korean") for p in fake_images.prefers)
    assert {p[2] for p in fake_images.prefers} == {"떡볶이 1", "호떡 1"}  # each card's own native name
    plain = quiz.GenOption(label="Road trip", sublabel="Every stop booked", emoji="x", color="#000000", image_query="x")
    assert quiz._photo_prefer(plain, Country(code="KR", name="South Korea")) == ("korea", "korean")
    assert quiz._country_names(Country(code="US", name="United States")) == ("america", "american", "americans", "usa")
    assert quiz._country_names(Country(code="MX", name="Mexico")) == ("mexico",)  # "mexican" is derived in images


async def test_local_reference_not_on_the_cards_does_not_count_as_local(scripted, fake_images):  # noqa: F811
    scripted.behavior = lambda user, schema, kw: payload(round_of(user), local_reference="Amtrak California Zephyr")
    assert (await quiz._generate(_snap())).local_ref == ""


async def test_other_players_recent_references_are_offered_as_avoid_list(scripted, fake_images):  # noqa: F811
    await quiz.start_quiz(seed_session(started=False).id)
    prompt = quiz._build_user_prompt(_snap(), must_local=True)
    assert "Recently used by other players from South Korea" in prompt and "Tteokbokki alley 1" in prompt


# --------------------------------------------------------------------------- soft retries vs. a waiting player


async def test_soft_issue_retry_is_skipped_when_the_player_is_already_waiting(scripted, fake_images):  # noqa: F811
    scripted.behavior = lambda user, schema, kw: payload(1, prompt="Plan it or wing it?")
    generated = await quiz._generate(_snap(), quiz._waiting())
    assert len(scripted.calls) == 1 and generated.stored.question.prompt == "Pick one:"


async def test_soft_issue_retry_is_dropped_once_the_player_starts_waiting(scripted, fake_images):  # noqa: F811
    retry_cancelled = asyncio.Event()

    async def behavior(user, schema, kw):
        if "Your previous draft broke these rules" in user:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                retry_cancelled.set()
                raise
        return payload(1, option_a={"label": "one two three four five six seven eight"})

    scripted.behavior = behavior
    wanted = asyncio.Event()
    task = asyncio.ensure_future(quiz._generate(_snap(), wanted))
    while len(scripted.calls) < 2:  # the corrective retry has started
        await asyncio.sleep(0.01)
    wanted.set()  # an answer arrives for this prefetched round
    generated = await asyncio.wait_for(task, 2)
    assert generated.stored.question.option_a.label.startswith("one two")
    await asyncio.wait_for(retry_cancelled.wait(), 1)


async def test_soft_issue_retry_still_runs_for_an_unclaimed_prefetch(scripted, fake_images):  # noqa: F811
    scripted.behavior = lambda user, schema, kw: payload(1, option_a={"label": "one two three four five six seven eight"})
    await quiz._generate(_snap(), asyncio.Event())
    assert len(scripted.calls) == 2


async def test_hard_issues_still_retry_while_the_player_waits(scripted, fake_images):  # noqa: F811
    drafts = iter([payload(1, option_b={"label": "Tteokbokki take 1"}), payload(1)])
    scripted.behavior = lambda user, schema, kw: next(drafts)
    await quiz._generate(_snap(), quiz._waiting())
    assert len(scripted.calls) == 2


async def test_a_waiting_player_hurries_the_photo_search(scripted, fake_images):  # noqa: F811
    await quiz._generate(_snap(), quiz._waiting())
    [hurry] = fake_images.hurries
    assert hurry.is_set()


# --------------------------------------------------------------------------- warm round 1


async def _settle_warm() -> None:
    pending = [e.task for e in quiz._warm.values() if not e.task.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_warm_round_one_is_claimed_by_exactly_one_session(scripted, fake_images):  # noqa: F811
    korea = Country(code="KR", name="South Korea")
    quiz.warm(korea)
    quiz.warm(korea)  # already on its way: no second generation
    await _settle_warm()
    assert scripted.rounds()[1] == 1

    first = seed_session(started=False)
    q1 = await quiz.start_quiz(first.id)
    assert scripted.rounds()[1] == 1  # served from the warm round, no new model call
    assert q1.index == 1 and store.require_session(first.id).current.question.id == q1.id

    second = seed_session(started=False)
    q2 = await quiz.start_quiz(second.id)
    assert q2.id != q1.id and scripted.rounds()[1] == 2  # each warm round is used once


async def test_warm_rounds_expire(scripted, fake_images, monkeypatch):  # noqa: F811
    quiz.warm(Country(code="JP", name="Japan"))
    await _settle_warm()
    monkeypatch.setattr(quiz, "WARM_TTL_S", -1)
    assert await quiz._take_warm("JP") is None


def test_api_warm_endpoint_accepts_a_country(client, fake_llm, fake_images):
    fake_llm.on("chat_json", lambda call: payload(round_of(call.user)))
    res = client.post("/api/quiz/warm", json={"country": {"code": "NG", "name": "Nigeria"}})
    assert res.status_code == 202
    assert client.post("/api/quiz/warm", json={"country": {"code": "N1"}}).status_code == 422


async def test_forget_session_cancels_its_prefetches(scripted, fake_images):  # noqa: F811
    rec = seed_session(started=False)
    await quiz.start_quiz(rec.id)
    assert any(k[0] == rec.id for k in quiz.prefetch_keys())
    await quiz.forget_session(rec.id)
    assert not any(k[0] == rec.id for k in quiz.prefetch_keys())


# --------------------------------------------------------------------------- concrete labels, matching sublabels

# Live game (South Korea, round 9): the labels only rated the thing; the songpyeon was in the sublabels.
SONGPYEON_ROUND = {
    "option_a": {"label": "One unforgettable centerpiece", "sublabel": "무지개 송편", "sublabel_en": "rainbow songpyeon",
                 "image_query": "rainbow songpyeon", "wiki_lang": "ko", "wiki_title": "송편"},
    "option_b": {"label": "Everyone's favorite classic", "sublabel": "깨송편", "sublabel_en": "sesame songpyeon",
                 "image_query": "sesame songpyeon", "wiki_lang": "ko", "wiki_title": "송편", "color": "#2f5bff"},
}
# Live game (South Korea, round 7): the sublabel named a restaurant guide, not the tasting menu.
MICHELIN_OPTION = {"label": "Seoul rooftop tasting menu", "sublabel": "미쉐린 가이드 서울",
                   "sublabel_en": "Michelin Guide Seoul", "image_query": "rooftop tasting menu",
                   "wiki_lang": "en", "wiki_title": "Tasting menu"}


def test_abstract_labels_are_a_soft_issue():
    soft = _soft(**SONGPYEON_ROUND)
    assert sum("doesn't name the thing on the card" in p for p in soft) == 2
    fixed = {side: {**opt, "label": label} for (side, opt), label in
             zip(SONGPYEON_ROUND.items(), ("Rainbow songpyeon", "Sesame songpyeon"))}
    assert not any("name the thing" in p or "names something" in p for p in _soft(**fixed))


def test_a_sublabel_naming_another_thing_is_a_soft_issue():
    soft = _soft(option_a=MICHELIN_OPTION)
    assert any("option A sublabel '미쉐린 가이드 서울' (Michelin Guide Seoul) names something other" in p for p in soft)
    # word order doesn't hide the lone shared place name
    assert any("names something other" in p for p in _soft(option_a={**MICHELIN_OPTION, "sublabel_en": "Seoul Michelin Guide"}))
    assert not any("names something" in p for p in _soft(option_a={
        **MICHELIN_OPTION, "sublabel": "루프탑 테이스팅 코스", "sublabel_en": "rooftop tasting course"}))


@pytest.mark.parametrize(
    ("label", "sublabel", "sublabel_en", "query"),
    [
        ("Bindaetteok at Gwangjang Market", "광장시장 빈대떡", "Gwangjang Market bindaetteok", "bindaetteok"),
        ("Noraebang with the whole team", "회식 노래방", "company dinner noraebang", "karaoke room"),
        ("Long padded coat every summer", "한여름 롱패딩", "midsummer long padding", "long padded coat"),
        ("Tteokbokki", "떡볶이", "Tteokbokki", "tteokbokki"),
        ("Lotte World Adventure", "롯데월드 어드벤처", "Lotte World Adventure", "Lotte World"),
        ("Busan Sajik baseball cheer", "사직구장 응원", "Sajik Stadium cheering", "baseball cheering"),
        ("Showa-era kissaten", "純喫茶", "pure coffee shop", "kissaten coffee shop"),
        ("Black-and-white Copacabana wave tiles", "calçadão de Copacabana", "Copacabana promenade",
         "Copacabana promenade"),
        ("Color-coded itinerary binder", None, None, "itinerary binder"),
    ],
)
def test_concrete_labels_with_matching_sublabels_pass(label, sublabel, sublabel_en, query):
    option = {"label": label, "sublabel": sublabel, "sublabel_en": sublabel_en, "image_query": query}
    assert not any("name the thing" in p or "names something" in p for p in _soft(option_a=option))


async def test_an_abstract_draft_is_rewritten_while_nobody_waits(scripted, fake_images):  # noqa: F811
    drafts = iter([payload(1, **SONGPYEON_ROUND), payload(1, option_a={"label": "Rainbow songpyeon"})])
    scripted.behavior = lambda user, schema, kw: next(drafts)
    generated = await quiz._generate(_snap(), asyncio.Event())
    assert "doesn't name the thing on the card" in scripted.calls[1].user
    assert generated.stored.question.option_a.label == "Rainbow songpyeon"


async def test_a_mismatched_sublabel_that_survives_is_dropped(scripted, fake_images):  # noqa: F811
    scripted.behavior = lambda user, schema, kw: payload(round_of(user), option_a=MICHELIN_OPTION)
    q = (await quiz._generate(_snap(), quiz._waiting())).stored.question  # player waiting: no retry
    assert len(scripted.calls) == 1
    assert (q.option_a.label, q.option_a.sublabel) == ("Seoul rooftop tasting menu", None)


@pytest.mark.parametrize(
    ("code", "name", "sublabel", "kept"),
    [
        ("US", "United States", "Street meat", False),  # English-speaking: the label is the name
        ("GB", "United Kingdom", "Chippy tea", False),
        ("NG", "Nigeria", "سوق بالوغون", False),  # a Lagos market in Arabic letters
        ("NG", "Nigeria", "Пuff-puff", False),  # half Cyrillic
        ("NG", "Nigeria", "aṣọ òkè", True),  # Yoruba, as written locally
        ("KR", "South Korea", "PC방 밤샘", True),
        ("JP", "Japan", "Jリーグ", True),
        ("BR", "Brazil", "pão de queijo", True),
    ],
)
async def test_sublabels_in_the_wrong_language_or_script_are_dropped(scripted, fake_images, code, name, sublabel, kept):  # noqa: F811
    country = Country(code=code, name=name)
    scripted.behavior = lambda user, schema, kw: payload(round_of(user), option_a={"label": "Market haul", "sublabel": sublabel,
                                                                                   "image_query": "market haul"})
    q = (await quiz._generate(quiz._Snapshot.of(seed_session(country=country, started=False)), quiz._waiting())).stored.question
    assert (q.option_a.sublabel == sublabel) is kept
    if code == "NG" and not kept:
        assert any("script locals don't use" in p for p in _soft(quiz._Snapshot.of(seed_session(country=country, started=False)),
                                                                  option_a={"sublabel": sublabel}))


def test_tidy_keeps_the_wikipedia_reference_only_when_complete():
    draft = quiz.GenQuestion.model_validate(payload(
        1, option_a={"wiki_lang": " KO ", "wiki_title": " 떡볶이 ", "sublabel_en": " rice cakes "},
        option_b={"wiki_lang": "en", "wiki_title": None},
    ))
    tidy = quiz._tidy(draft)
    assert (tidy.option_a.wiki_lang, tidy.option_a.wiki_title, tidy.option_a.sublabel_en) == ("ko", "떡볶이", "rice cakes")
    assert (tidy.option_b.wiki_lang, tidy.option_b.wiki_title) == (None, None)


def test_system_prompt_asks_for_concrete_labels_and_wikipedia_articles():
    prompt = quiz._system_prompt(Country(code="BR", name="Brazil"))
    assert "One unforgettable centerpiece" in prompt and "미쉐린 가이드 서울" in prompt
    assert "wiki_lang + wiki_title" in prompt and "sublabel_en" in prompt
    assert "Every stop booked" not in prompt  # the old abstract US example is gone
