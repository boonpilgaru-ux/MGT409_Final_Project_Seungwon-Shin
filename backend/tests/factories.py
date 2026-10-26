"""Builders for realistic test data. Import in tests: ``from tests.factories import seed_session, ...``."""

from __future__ import annotations

from app import store
from app.models import (
    QUIZ_TOTAL,
    ContextItem,
    Country,
    Design,
    Option,
    OptionImage,
    Product,
    ProductSpec,
    Profile,
    Question,
    Taste,
    TasteColor,
    Trait,
)
from app.store import SessionRecord, StoredAnswer, StoredContext, StoredQuestion

KOREA = Country(code="KR", name="South Korea")


def depth_for_round(index: int) -> str:
    return "broad" if index <= 4 else "focused" if index <= 10 else "specific"

SIMPLE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000">'
    '<circle cx="500" cy="500" r="300" fill="#ff3b30"/>'
    '<text x="500" y="520" text-anchor="middle" font-family="sans-serif" font-size="96" fill="#141414">TEE</text>'
    "</svg>"
)


def make_option(label: str = "Tteokbokki", sublabel: str | None = "떡볶이", *, emoji: str = "🌶️",
                color: str = "#ff3b30", image: OptionImage | None = None) -> Option:
    return Option(label=label, sublabel=sublabel, emoji=emoji, color=color, image=image)


def make_question(index: int = 1, *, qid: str | None = None) -> Question:
    return Question(
        id=qid or f"q{index}-{store.new_id()}",
        index=index,
        total=QUIZ_TOTAL,
        prompt="Pick one. Forever.",
        depth=depth_for_round(index),
        option_a=make_option("Tteokbokki", "떡볶이", emoji="🌶️", color="#ff3b30"),
        option_b=make_option("Hotteok", "호떡", emoji="🥞", color="#2f5bff"),
    )


def make_stored_question(index: int = 1, dimension: str = "comfort vs thrill") -> StoredQuestion:
    return StoredQuestion(question=make_question(index), dimension=dimension)


def make_answers(n: int, pattern: str = "AB") -> list[StoredAnswer]:
    return [
        StoredAnswer(question=make_question(i), dimension=f"dimension {i}", choice=pattern[(i - 1) % len(pattern)])
        for i in range(1, n + 1)
    ]


def make_profile() -> Profile:
    return Profile(
        archetype="The Night-Market Curator",
        tagline="Cozy chaos, curated.",
        summary="You pick comfort with a twist and love a good story behind every object.",
        traits=[
            Trait(name="Social energy", score=62, low_label="Recharge solo", high_label="Life of the party",
                  description="Warm in small groups."),
            Trait(name="Novelty", score=78, low_label="Classic", high_label="Experimental",
                  description="You chase the new stall."),
            Trait(name="Planning", score=35, low_label="Spontaneous", high_label="Planner",
                  description="Loose plans, strong instincts."),
            Trait(name="Boldness", score=70, low_label="Subtle", high_label="Loud",
                  description="Statement, but witty."),
            Trait(name="Nostalgia", score=55, low_label="Future-facing", high_label="Retro",
                  description="Old-school with new tricks."),
        ],
        taste=Taste(
            colors=[TasteColor(hex="#FF3B30", name="Chili red"), TasteColor(hex="#2F5BFF", name="Subway blue"),
                    TasteColor(hex="#FAF7F2", name="Rice paper"), TasteColor(hex="#141414", name="Ink")],
            aesthetics=["retro street", "hand-lettered signage"],
            motifs=["street food", "neon"],
            typography="chunky rounded sans with hand-drawn accents",
            fits=["oversized", "boxy"],
            avoid=["big logos"],
        ),
        cultural_roots="Seoul night markets and 90s K-pop nostalgia.",
        values=["warmth", "curiosity", "humor"],
        tshirt_brief="Bold flat illustration of a night-market stall with Hangul signage, two colors on cream.",
    )


def make_spec(**overrides) -> ProductSpec:
    return ProductSpec(**overrides)


def make_design(design_id: str | None = None, svg: str = SIMPLE_SVG) -> Design:
    return Design(
        id=design_id or store.new_id(),
        title="Night Market",
        concept="A glowing tteokbokki stall under neon Hangul.",
        svg=svg,
        palette=["#ff3b30", "#141414"],
        recommended_spec=ProductSpec(shirt_color="#faf7f2", fit="oversized"),
    )


def make_product(i: int = 0, origin: str = "global") -> Product:
    return Product(
        id=f"p{i}", title=f"Graphic Tee {i}", brand="Brand", retailer="Shop", url=f"https://shop.example/tee-{i}",
        image_url=None, price="$30", currency="USD", origin=origin, why="Matches your retro streak.",
    )


def make_context(kind: str = "text", text: str = "I shoot film and collect zines.") -> StoredContext:
    item = ContextItem(id=store.new_id(), kind=kind, name="Notes", chars=len(text), status="ok",
                       summary="Film photographer who loves zines.", error=None)
    return StoredContext(item=item, text=text, source=None)


def seed_session(
    *,
    country: Country = KOREA,
    answered: int = 0,
    started: bool = True,
    done: bool = False,
    profile: bool = False,
    designs: int = 0,
    context: int = 0,
    hypothesis: str = "",
) -> SessionRecord:
    """Persist a session in a given state and return it.

    ``done=True`` -> all 15 answers, no current question, quiz_done. Otherwise ``answered`` answers
    and (if ``started``) a current question for the next round.
    """
    rec = SessionRecord(id=store.new_id(), country=country, hypothesis=hypothesis)
    if done:
        rec.answers = make_answers(QUIZ_TOTAL)
        rec.quiz_done = True
    else:
        rec.answers = make_answers(answered)
        if started:
            rec.current = make_stored_question(answered + 1)
    if profile:
        rec.profile = make_profile()
    rec.designs = [make_design() for _ in range(designs)]
    rec.context = [make_context() for _ in range(context)]
    store.save_session(rec)
    return rec
