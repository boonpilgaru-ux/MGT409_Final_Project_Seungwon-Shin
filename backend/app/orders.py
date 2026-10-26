"""Orders: pricing, creation, demo checkout, print file (SPEC section 5, orders.py).

routes.py calls (all sync):
    orders.price_for(spec)                                  -> Price      (also POST /api/quote)
    orders.create_order(session_id, design_id, spec)        -> Order
    orders.get_order(order_id)                              -> Order | None
    orders.public_view(order)                               -> Order      (GET /api/orders/{id})
    orders.request_order(order_id, customer, token=None)    -> (Order, new token | None)
    orders.print_svg(order)                                 -> str
Persistence: ``store.save_order(order, session_id)`` / ``store.get_order(order_id)``.

Privacy of the shareable order page: anyone with the link may read the order and place the
demo request once, but the public read never carries the buyer's email or address (email masked,
address "(hidden)"; same SPEC shape), and only the browser that placed the request can update it:
that request returns a one-time ``X-Order-Token`` (only its SHA-256 is stored), which updates
must send back. The full customer stays in the stored order for fulfilment and print export.

Money math uses ``Decimal`` so every breakdown line is an exact cent amount and the lines always
sum to ``total`` (floats only appear at the model boundary).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
from decimal import ROUND_HALF_UP, Decimal

from . import design, store
from .errors import BadInputError, ConflictError, ForbiddenError, NotFoundError
from .models import Customer, Order, Price, PriceLine, ProductSpec

__all__ = [
    "price_for", "create_order", "get_order", "public_view", "request_order", "print_svg", "discount_rate",
    "mask_email", "ORDER_TOKEN_HEADER", "MAX_ORDERS_PER_SESSION",
]  # fmt: skip

log = logging.getLogger("teedentity.orders")

ORDER_TOKEN_HEADER = "X-Order-Token"
MAX_ORDERS_PER_SESSION = 100  # each order snapshots a design SVG (up to 200 KB)
ADDRESS_HIDDEN = "(hidden)"
_request_lock = threading.Lock()  # read-modify-write of one order's request (sync routes run in threads)

# --------------------------------------------------------------------------- price table (USD)

MATERIAL_PRICES: dict[str, tuple[str, Decimal]] = {
    "cotton": ("Cotton tee", Decimal(12)),
    "organic_cotton": ("Organic cotton tee", Decimal(16)),
    "heavyweight_cotton": ("Heavyweight cotton tee", Decimal(18)),
    "tri_blend": ("Tri-blend tee", Decimal(15)),
    "linen_blend": ("Linen-blend tee", Decimal(22)),
    "recycled_poly": ("Recycled poly tee", Decimal(14)),
}
PRINT_PRICES: dict[str, tuple[str, Decimal]] = {
    "dtg": ("DTG print", Decimal(8)),
    "screen": ("Screen print", Decimal(6)),
    "embroidery": ("Embroidery", Decimal(12)),
    "puff": ("Puff print", Decimal(10)),
}
FRONT_AND_BACK_FEE = Decimal(7)  # per shirt
LONG_SLEEVE_FEE = Decimal(5)  # per shirt
XXL_FEE = Decimal(3)  # per shirt
SCREEN_SETUP_FEE = Decimal(30)  # once per order
QUANTITY_DISCOUNTS: tuple[tuple[int, Decimal], ...] = ((25, Decimal("0.20")), (10, Decimal("0.10")))

_CENT = Decimal("0.01")


def _cents(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def discount_rate(quantity: int) -> Decimal:
    """Quantity discount: 20% at >= 25 shirts, 10% at >= 10, else 0."""
    for threshold, rate in QUANTITY_DISCOUNTS:
        if quantity >= threshold:
            return rate
    return Decimal(0)


def price_for(spec: ProductSpec) -> Price:
    """Price estimate in USD, every amount rounded to cents.

    Per shirt: material base + print method + front_and_back 7 + long sleeve 5 + XXL 3.
    Per order: screen printing adds a one-off 30 setup fee (never discounted).
    Discount: 10% at >= 10 shirts, 20% at >= 25, applied to the per-shirt price.

    ``unit`` = per-shirt price after discount (excludes the one-off setup fee);
    ``total`` = ``unit * quantity`` + setup; ``breakdown`` lines (charges positive, discount
    negative) sum exactly to ``total``.
    """
    qty = spec.quantity
    per_shirt: list[tuple[str, Decimal]] = [MATERIAL_PRICES[spec.material], PRINT_PRICES[spec.print_method]]
    if spec.placement == "front_and_back":
        per_shirt.append(("Front + back print", FRONT_AND_BACK_FEE))
    if spec.sleeve == "long":
        per_shirt.append(("Long sleeves", LONG_SLEEVE_FEE))
    if spec.size == "XXL":
        per_shirt.append(("XXL size", XXL_FEE))

    unit_before = sum((amount for _, amount in per_shirt), Decimal(0))
    rate = discount_rate(qty)
    unit_discount = _cents(unit_before * rate)
    unit = unit_before - unit_discount
    setup = SCREEN_SETUP_FEE if spec.print_method == "screen" else Decimal(0)

    suffix = f" × {qty}" if qty > 1 else ""
    lines = [PriceLine(label=f"{label}{suffix}", amount=float(_cents(amount * qty))) for label, amount in per_shirt]
    if unit_discount:
        lines.append(
            PriceLine(label=f"Quantity discount ({int(rate * 100)}% off)", amount=float(-_cents(unit_discount * qty)))
        )
    if setup:
        lines.append(PriceLine(label="Screen setup (one-time)", amount=float(setup)))

    total = _cents(unit * qty + setup)
    return Price(unit=float(_cents(unit)), total=float(total), currency="USD", breakdown=lines)


# --------------------------------------------------------------------------- orders


def create_order(session_id: str, design_id: str, spec: ProductSpec) -> Order:
    """Create and persist a ``draft`` order that snapshots the design, spec, price, the profile's
    archetype (or None) and the session country.

    Raises:
        NotFoundError: unknown session or design (routes.py pre-checks both).
        ConflictError: the session already has MAX_ORDERS_PER_SESSION orders.
    """
    rec = store.require_session(session_id)
    chosen = rec.find_design(design_id)
    if chosen is None:
        raise NotFoundError("That design doesn't exist in this session.")
    if store.count_orders(session_id) >= MAX_ORDERS_PER_SESSION:
        raise ConflictError(
            f"You've made {MAX_ORDERS_PER_SESSION} order links already. Reuse one of them, or start over."
        )
    order = Order(
        id=store.new_id(),
        created_at=store.now_iso(),
        status="draft",
        design=chosen.model_copy(deep=True),
        spec=spec,
        price=price_for(spec),
        profile_archetype=rec.profile.archetype if rec.profile else None,
        country=rec.country,
        customer=None,
    )
    store.save_order(order, session_id)
    log.info("order %s created (session %s, design %s)", order.id, session_id, design_id)
    return order


def get_order(order_id: str) -> Order | None:
    """Load an order, or None if unknown (routes.py turns None into 404)."""
    return store.get_order(order_id)


# Stricter than the model's loose check: one "@", a dotted domain, no spaces/control chars/brackets.
_EMAIL_RE = re.compile(r"[^@\s<>()\[\],;:\"]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9-]+)+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_customer(customer: Customer) -> Customer:
    name = customer.name.strip()
    email = customer.email.strip()
    address = customer.address.strip()
    if not name or not address:
        raise BadInputError("Please fill in your name and address.")
    if _CONTROL_RE.search(name + email + address) or "\n" in name or "\n" in email:
        raise BadInputError("Please remove unusual characters from the form.")
    if not _EMAIL_RE.fullmatch(email) or ".." in email:
        raise BadInputError("Please enter a valid email address.")
    return Customer(name=name, email=email, address=address)


def mask_email(email: str) -> str:
    """'mina@example.com' -> 'm•••@example.com' (idempotent; still passes the loose email check)."""
    user, _, domain = email.partition("@")
    return f"{user[:1]}•••@{domain}" if user and domain else "•••"


def public_view(order: Order) -> Order:
    """The order as anyone with the share link sees it: name kept, email masked, address hidden."""
    customer = order.customer
    if customer is None:
        return order
    masked = Customer.model_construct(name=customer.name, email=mask_email(customer.email), address=ADDRESS_HIDDEN)
    return order.model_copy(update={"customer": masked})


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def request_order(order_id: str, customer: Customer, token: str | None = None) -> tuple[Order, str | None]:
    """Demo checkout: attach ``customer``, set status ``requested``, persist. No payment is taken.

    The first request mints a requester token (returned once; only its hash is stored). Updating
    an already-requested order needs that token, so a stranger holding the share link can't
    silently replace the buyer's details. Returns ``(order with full customer, new token | None)``.

    Raises:
        NotFoundError: unknown order.
        ForbiddenError: already requested, and ``token`` doesn't match the requester's.
        BadInputError: customer fails validation beyond the model's loose email check.
    """
    cleaned = _clean_customer(customer)
    with _request_lock:
        order = store.get_order(order_id)
        if order is None:
            raise NotFoundError("Order not found.")
        new_token: str | None = None
        if order.status == "requested":
            stored = store.get_order_requester_hash(order_id)
            if not (stored and token and hmac.compare_digest(stored, _token_hash(token))):
                raise ForbiddenError(
                    "This order was already requested from another browser, so only that browser can "
                    "change it. Make your own shirt to place a new request."
                )
            requester_hash = stored
        else:
            new_token = secrets.token_urlsafe(24)
            requester_hash = _token_hash(new_token)
        updated = order.model_copy(update={"customer": cleaned, "status": "requested"})
        store.save_order(updated, requester_hash=requester_hash)  # keeps the hidden session id too
    log.info("order %s %s", order_id, "requested" if new_token else "request updated")
    return updated, new_token


def print_svg(order: Order) -> str:
    """Print-ready SVG for GET /api/orders/{id}/print.svg: the design SVG, re-sanitized defensively."""
    try:
        return design.sanitize_svg(order.design.svg)
    except ValueError as err:
        log.warning("order %s: stored design failed re-sanitization: %s", order.id, err)
        raise ConflictError("This design can't be exported as a print file.") from err
