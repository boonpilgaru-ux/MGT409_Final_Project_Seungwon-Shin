"""All public API models (SPEC section 4) plus request/response bodies.

Single source of truth for the wire format. ``frontend/src/types.ts`` mirrors these with the
exact same field names — change both together.

Internal-only (hidden) session data lives in ``store.SessionRecord``, not here.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal, NamedTuple

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------- shared types

Depth = Literal["broad", "focused", "specific"]
Choice = Literal["A", "B"]
ContextKind = Literal["file", "url", "text"]
ContextStatus = Literal["ok", "limited", "error"]
Origin = Literal["local", "global", "indie"]
PickMatchSource = Literal["product_page", "search_result"]
Fit = Literal["regular", "oversized", "slim", "boxy", "cropped"]
Material = Literal["cotton", "organic_cotton", "heavyweight_cotton", "tri_blend", "linen_blend", "recycled_poly"]
Neckline = Literal["crew", "v_neck", "mock_neck"]
Sleeve = Literal["short", "long", "sleeveless"]
Size = Literal["XS", "S", "M", "L", "XL", "XXL"]
PrintMethod = Literal["dtg", "screen", "embroidery", "puff"]
Placement = Literal["front_center", "left_chest", "back", "front_and_back"]
OrderStatus = Literal["draft", "requested"]

QUIZ_TOTAL = 10

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _normalize_hex(value: str) -> str:
    """Accept ``#abc`` / ``abc`` / ``#AABBCC`` and return lowercase ``#aabbcc``."""
    match = _HEX_RE.match(value.strip())
    if not match:
        raise ValueError(f"not a hex color: {value!r}")
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits.lower()


def _round_clamp_score(value: Any) -> Any:
    """LLMs sometimes emit 72.5 or 105; coerce to an int in [0, 100].

    ``1e999`` / ``Infinity`` parse to float inf, and round(inf) raises OverflowError, which pydantic
    doesn't turn into a ValidationError (so JSON repair never ran and the request 500'd): clamp
    those too. NaN is left alone and fails validation normally.
    """
    if isinstance(value, float) and math.isinf(value):
        return 100 if value > 0 else 0
    if isinstance(value, (int, float)) and not isinstance(value, bool) and not (
        isinstance(value, float) and math.isnan(value)
    ):
        return max(0, min(100, int(round(value))))
    return value


HexColor = Annotated[str, AfterValidator(_normalize_hex)]
Score = Annotated[int, BeforeValidator(_round_clamp_score), Field(ge=0, le=100)]


class ApiModel(BaseModel):
    """Base for all wire models: ignore unknown keys, strip surrounding whitespace on strings."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# --------------------------------------------------------------------------- section 4 models


class Country(ApiModel):
    code: str = Field(min_length=2, max_length=2, description="ISO 3166-1 alpha-2, uppercase")
    name: str = Field(min_length=1, max_length=80)

    @field_validator("code")
    @classmethod
    def _upper_alpha(cls, v: str) -> str:
        if not v.isalpha():
            raise ValueError("country code must be two letters")
        return v.upper()


class OptionImage(ApiModel):
    url: str
    credit: str
    source_url: str | None = None


class Option(ApiModel):
    label: str = Field(description="<= 7 words, English")
    sublabel: str | None = Field(default=None, description="<= 12 words; may hold native-script term")
    emoji: str
    color: HexColor = Field(description="fallback gradient color")
    image: OptionImage | None = None


class Question(ApiModel):
    id: str
    index: int = Field(ge=1, description="1-based round number")
    total: int = QUIZ_TOTAL
    prompt: str = Field(description="<= 12 words")
    depth: Depth
    option_a: Option
    option_b: Option


class Answer(ApiModel):
    question_id: str
    choice: Choice


class ContextItem(ApiModel):
    id: str
    kind: ContextKind
    name: str
    chars: int = Field(ge=0, description="characters of extracted text kept")
    status: ContextStatus
    summary: str
    error: str | None = None


class Trait(ApiModel):
    name: str
    score: Score
    low_label: str
    high_label: str
    description: str


class TasteColor(ApiModel):
    hex: HexColor
    name: str


class Taste(ApiModel):
    colors: list[TasteColor] = Field(description="4-6 swatches")
    aesthetics: list[str]
    motifs: list[str]
    typography: str
    fits: list[str]
    avoid: list[str]


class Profile(ApiModel):
    archetype: str
    tagline: str
    summary: str
    traits: list[Trait] = Field(description="5-7 traits")
    taste: Taste
    cultural_roots: str
    values: list[str]
    tshirt_brief: str


class Product(ApiModel):
    id: str
    title: str
    brand: str
    retailer: str
    url: str
    image_url: str | None = None
    price: str | None = None
    currency: str | None = None
    origin: Origin
    why: str
    pick_match: str | None = Field(default=None, description="a chosen game pick matched in page or search title")
    pick_match_source: PickMatchSource | None = None


class ProductSpec(ApiModel):
    shirt_color: HexColor = "#ffffff"
    fit: Fit = "regular"
    material: Material = "cotton"
    neckline: Neckline = "crew"
    sleeve: Sleeve = "short"
    size: Size = "M"
    print_method: PrintMethod = "dtg"
    placement: Placement = "front_center"
    quantity: int = Field(default=1, ge=1, le=50)
    notes: str = Field(default="", max_length=500)


class Design(ApiModel):
    id: str
    title: str
    concept: str
    svg: str = Field(description="sanitized standalone SVG, viewBox 0 0 1000 1000")
    palette: list[str]
    recommended_spec: ProductSpec


class PriceLine(ApiModel):
    label: str
    amount: float


class Price(ApiModel):
    unit: float
    total: float
    currency: Literal["USD"] = "USD"
    breakdown: list[PriceLine]


class Customer(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    address: str = Field(min_length=1, max_length=1000)

    @field_validator("email")
    @classmethod
    def _loose_email(cls, v: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("enter a valid email address")
        return v


class Order(ApiModel):
    id: str
    created_at: str = Field(description="ISO 8601 UTC")
    status: OrderStatus
    design: Design
    spec: ProductSpec
    price: Price
    profile_archetype: str | None = None
    country: Country | None = None
    customer: Customer | None = None


class AnsweredQuestion(ApiModel):
    question: Question
    choice: Choice


class UsageSummary(ApiModel):
    input_tokens: int = Field(default=0, ge=0, description="total input tokens, including cached input")
    cached_input_tokens: int = Field(default=0, ge=0, description="cached input tokens, included in input_tokens")
    output_tokens: int = Field(default=0, ge=0)
    request_count: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0, description="estimated at course Luna token rates")


class SessionState(ApiModel):
    id: str
    country: Country
    answers: list[AnsweredQuestion]
    current_question: Question | None = None
    quiz_done: bool
    context: list[ContextItem]
    profile: Profile | None = None
    designs: list[Design]


# --------------------------------------------------------------------------- request / response bodies


class HealthResponse(ApiModel):
    ok: bool
    portkey_key_set: bool
    models: dict[str, str]


class CreateSessionRequest(ApiModel):
    country: Country


class WarmRequest(ApiModel):
    """Extra (not in SPEC): POST /api/quiz/warm pre-generates round 1 for a country."""

    country: Country


class AnswerResult(ApiModel):
    next: Question | None = None
    done: bool


class ContextResponse(ApiModel):
    items: list[ContextItem]


class ShopResponse(ApiModel):
    products: list[Product]


class DesignGenerateRequest(ApiModel):
    feedback: str | None = Field(default=None, max_length=500)
    base_design_id: str | None = None
    count: int = Field(default=3, ge=1, le=3)

    @field_validator("feedback")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        return v or None


class DesignResponse(ApiModel):
    designs: list[Design]


class CreateOrderRequest(ApiModel):
    session_id: str
    design_id: str
    spec: ProductSpec


# --------------------------------------------------------------------------- internal helpers

# "About you" limits (SPEC section 2, step 3). Enforced in routes.py; per session, all kinds counted.
CONTEXT_MAX_FILES = 5
CONTEXT_MAX_URLS = 5
CONTEXT_MAX_NOTES = 5  # free-text "anything else" items (each POST with text adds one)
CONTEXT_MAX_FILE_BYTES = 10 * 1024 * 1024
CONTEXT_MAX_TEXT_CHARS = 20_000
CONTEXT_ALLOWED_EXTENSIONS = (".pdf", ".docx", ".txt", ".md", ".markdown")


class UploadedFile(NamedTuple):
    """A file received by POST /api/context/{id}, already read into memory (<= 10 MB)."""

    filename: str
    data: bytes
    content_type: str
