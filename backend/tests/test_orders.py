"""orders.py: pricing math, order lifecycle (create / get / request), print file export."""

from __future__ import annotations

import itertools
import xml.etree.ElementTree as ET
from decimal import Decimal

import pytest

from app import orders, store
from app.errors import BadInputError, NotFoundError
from app.models import Customer, ProductSpec
from tests.factories import make_design, seed_session


def _spec(**kw) -> ProductSpec:
    return ProductSpec(**kw)


def _sum(price) -> Decimal:
    return sum((Decimal(str(line.amount)) for line in price.breakdown), Decimal(0))


# --------------------------------------------------------------------------- pricing


def test_base_price_cotton_dtg():
    price = orders.price_for(_spec())
    assert (price.unit, price.total, price.currency) == (20.0, 20.0, "USD")
    assert [(line.label, line.amount) for line in price.breakdown] == [("Cotton tee", 12.0), ("DTG print", 8.0)]


@pytest.mark.parametrize(
    ("material", "base"),
    [("cotton", 12), ("organic_cotton", 16), ("heavyweight_cotton", 18), ("tri_blend", 15),
     ("linen_blend", 22), ("recycled_poly", 14)],
)
def test_material_base_prices(material, base):
    assert orders.price_for(_spec(material=material, print_method="dtg")).unit == base + 8


@pytest.mark.parametrize(("method", "fee"), [("dtg", 8), ("screen", 6), ("embroidery", 12), ("puff", 10)])
def test_print_method_prices(method, fee):
    assert orders.price_for(_spec(print_method=method)).unit == 12 + fee


def test_all_per_shirt_extras():
    spec = _spec(material="heavyweight_cotton", print_method="embroidery", placement="front_and_back",
                 sleeve="long", size="XXL")
    price = orders.price_for(spec)
    assert price.unit == price.total == 18 + 12 + 7 + 5 + 3
    labels = [line.label for line in price.breakdown]
    assert labels == ["Heavyweight cotton tee", "Embroidery", "Front + back print", "Long sleeves", "XXL size"]


@pytest.mark.parametrize("placement", ["front_center", "left_chest", "back"])
def test_single_side_placements_cost_nothing_extra(placement):
    assert orders.price_for(_spec(placement=placement)).total == 20.0


def test_screen_setup_is_once_per_order_and_not_per_unit():
    one = orders.price_for(_spec(print_method="screen"))
    assert one.unit == 18.0 and one.total == 48.0
    assert one.breakdown[-1].label == "Screen setup (one-time)" and one.breakdown[-1].amount == 30.0
    many = orders.price_for(_spec(print_method="screen", quantity=5))
    assert many.unit == 18.0 and many.total == 5 * 18 + 30


@pytest.mark.parametrize(
    ("quantity", "unit", "total"),
    [(1, 20.0, 20.0), (9, 20.0, 180.0), (10, 18.0, 180.0), (24, 18.0, 432.0), (25, 16.0, 400.0),
     (50, 16.0, 800.0)],
)
def test_quantity_discount_thresholds(quantity, unit, total):
    price = orders.price_for(_spec(quantity=quantity))
    assert (price.unit, price.total) == (unit, total)


def test_discount_line_and_cents_rounding():
    # tri_blend 15 + dtg 8 = 23; 10% off -> 2.30 off per shirt -> 20.70 unit.
    price = orders.price_for(_spec(material="tri_blend", quantity=10))
    assert price.unit == 20.70 and price.total == 207.0
    assert ("Quantity discount (10% off)", -23.0) in [(line.label, line.amount) for line in price.breakdown]
    assert price.breakdown[0].label == "Tri-blend tee × 10"


def test_discount_does_not_apply_to_screen_setup():
    # linen 22 + screen 6 + both sides 7 + long 5 + XXL 3 = 43; 20% off at 50 -> 34.40; + 30 setup.
    spec = _spec(material="linen_blend", print_method="screen", placement="front_and_back", sleeve="long",
                 size="XXL", quantity=50)
    price = orders.price_for(spec)
    assert price.unit == 34.40
    assert price.total == 34.40 * 50 + 30 == 1750.0


def test_breakdown_always_sums_to_total():
    combos = itertools.product(
        list(orders.MATERIAL_PRICES), list(orders.PRINT_PRICES), ["front_center", "front_and_back"],
        ["short", "long"], ["M", "XXL"], [1, 3, 9, 10, 11, 24, 25, 37, 50],
    )
    for material, method, placement, sleeve, size, qty in combos:
        spec = _spec(material=material, print_method=method, placement=placement, sleeve=sleeve, size=size,
                     quantity=qty)
        price = orders.price_for(spec)
        assert _sum(price) == Decimal(str(price.total)), spec
        setup = 30 if method == "screen" else 0
        assert Decimal(str(price.unit)) * qty + setup == Decimal(str(price.total)), spec
        for value in [price.unit, price.total, *(line.amount for line in price.breakdown)]:
            assert Decimal(str(value)) == Decimal(str(value)).quantize(Decimal("0.01"))


def test_quote_endpoint(client):
    res = client.post("/api/quote", json={"material": "organic_cotton", "print_method": "puff", "quantity": 25})
    assert res.status_code == 200
    body = res.json()
    assert body["unit"] == 20.8 and body["total"] == 520.0 and body["currency"] == "USD"


# --------------------------------------------------------------------------- lifecycle


def test_create_get_request_print_flow(client):
    rec = seed_session(done=True, profile=True, designs=2)
    design = rec.designs[1]
    spec = {"shirt_color": "#1A1A1A", "fit": "boxy", "material": "heavyweight_cotton", "quantity": 2,
            "notes": "Gift for my sister"}
    res = client.post("/api/orders", json={"session_id": rec.id, "design_id": design.id, "spec": spec})
    assert res.status_code == 200
    order = res.json()
    assert order["status"] == "draft" and order["customer"] is None
    assert order["design"] == design.model_dump(mode="json")
    assert order["spec"]["shirt_color"] == "#1a1a1a" and order["spec"]["notes"] == "Gift for my sister"
    assert order["price"]["total"] == 52.0 and order["price"]["unit"] == 26.0
    assert order["profile_archetype"] == rec.profile.archetype
    assert order["country"] == {"code": "KR", "name": "South Korea"}
    assert len(order["id"]) == 12 and order["created_at"].endswith("+00:00")

    fetched = client.get(f"/api/orders/{order['id']}")
    assert fetched.status_code == 200 and fetched.json() == order

    customer = {"name": "  Mina Kim ", "email": "mina@example.com", "address": "1 Sejong-daero, Seoul"}
    res = client.post(f"/api/orders/{order['id']}/request", json=customer)
    assert res.status_code == 200
    requested = res.json()
    assert requested["status"] == "requested"
    assert requested["customer"] == {"name": "Mina Kim", "email": "mina@example.com",
                                     "address": "1 Sejong-daero, Seoul"}
    assert requested["price"] == order["price"] and requested["design"] == order["design"]
    assert store.get_order_session_id(order["id"]) == rec.id  # hidden link survives the update
    token = res.headers["x-order-token"]
    assert len(token) >= 24 and token not in store._read_one("SELECT data FROM orders WHERE id = ?", (order["id"],))[0]

    # The share link is public: anyone can read it, but never the buyer's email or address.
    public = client.get(f"/api/orders/{order['id']}").json()
    assert public["customer"] == {"name": "Mina Kim", "email": "m•••@example.com", "address": "(hidden)"}
    assert public["status"] == "requested"

    # A stranger with the link can't overwrite the request...
    stranger = client.post(f"/api/orders/{order['id']}/request", json={**customer, "address": "Attacker drop"})
    assert stranger.status_code == 403 and "already requested" in stranger.json()["detail"]
    wrong = client.post(f"/api/orders/{order['id']}/request", json=customer, headers={"X-Order-Token": "guess"})
    assert wrong.status_code == 403
    # ...but the requester (who holds the token) can update it.
    again = client.post(
        f"/api/orders/{order['id']}/request", json={**customer, "address": "2 New St"}, headers={"X-Order-Token": token}
    )
    assert again.status_code == 200 and again.json()["customer"]["address"] == "2 New St"
    assert "x-order-token" not in again.headers  # the token is minted once
    stored = store.get_order(order["id"])  # full details persist for fulfilment
    assert stored.customer.email == "mina@example.com" and stored.customer.address == "2 New St"

    svg = client.get(f"/api/orders/{order['id']}/print.svg")
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml")
    root = ET.fromstring(svg.text)
    assert root.tag == "{http://www.w3.org/2000/svg}svg" and root.get("viewBox") == "0 0 1000 1000"


def test_order_snapshot_is_independent_of_later_session_changes():
    rec = seed_session(done=True, profile=True, designs=1)
    order = orders.create_order(rec.id, rec.designs[0].id, _spec())
    live = store.require_session(rec.id)
    live.designs = []
    live.profile = None
    store.save_session(live)
    loaded = orders.get_order(order.id)
    assert loaded == order and loaded.profile_archetype == rec.profile.archetype


def test_create_order_without_profile_has_null_archetype():
    rec = seed_session(done=True, profile=False, designs=1)
    order = orders.create_order(rec.id, rec.designs[0].id, _spec())
    assert order.profile_archetype is None and order.country.code == "KR"


def test_create_order_unknown_design_or_session():
    rec = seed_session(done=True, profile=True, designs=1)
    with pytest.raises(NotFoundError):
        orders.create_order(rec.id, "nope", _spec())
    with pytest.raises(NotFoundError):
        orders.create_order("missing", rec.designs[0].id, _spec())


def test_mask_email_is_idempotent_and_public_view_keeps_the_shape():
    assert orders.mask_email("mina@example.com") == "m•••@example.com"
    assert orders.mask_email(orders.mask_email("mina@example.com")) == "m•••@example.com"
    rec = seed_session(done=True, profile=True, designs=1)
    order = orders.create_order(rec.id, rec.designs[0].id, _spec())
    assert orders.public_view(order) == order  # no customer yet: unchanged
    requested, _ = orders.request_order(order.id, Customer(name="Mina", email="mina@example.com", address="1 St"))
    view = orders.public_view(requested)
    assert Customer.model_validate(view.customer.model_dump()).address == "(hidden)"  # still a valid Customer


def test_orders_per_session_are_capped(monkeypatch):
    from app.errors import ConflictError

    monkeypatch.setattr(orders, "MAX_ORDERS_PER_SESSION", 2)
    rec = seed_session(done=True, profile=True, designs=1)
    other = seed_session(done=True, profile=True, designs=1)
    orders.create_order(rec.id, rec.designs[0].id, _spec())
    orders.create_order(rec.id, rec.designs[0].id, _spec())
    with pytest.raises(ConflictError, match="order links"):
        orders.create_order(rec.id, rec.designs[0].id, _spec())
    assert orders.create_order(other.id, other.designs[0].id, _spec())  # per session, not global
    assert store.count_orders(rec.id) == 2


def test_get_and_request_unknown_order():
    assert orders.get_order("missing") is None
    with pytest.raises(NotFoundError):
        orders.request_order("missing", Customer(name="A", email="a@example.com", address="x"))


@pytest.mark.parametrize(
    "email", ["a@b", "a b@example.com", "a@@example.com", "@example.com", "a@example..com", "a@-example.com"]
)
def test_request_rejects_bad_email(client, email):
    rec = seed_session(done=True, profile=True, designs=1)
    order = orders.create_order(rec.id, rec.designs[0].id, _spec())
    res = client.post(f"/api/orders/{order.id}/request", json={"name": "A", "email": email, "address": "x"})
    assert res.status_code == 422 and isinstance(res.json()["detail"], str)
    assert orders.get_order(order.id).status == "draft"


def test_request_rejects_control_characters():
    rec = seed_session(done=True, profile=True, designs=1)
    order = orders.create_order(rec.id, rec.designs[0].id, _spec())
    with pytest.raises(BadInputError):
        orders.request_order(order.id, Customer(name="Mina\x07", email="m@example.com", address="x"))


def test_request_order_validation_via_api(client):
    rec = seed_session(done=True, profile=True, designs=1)
    order = orders.create_order(rec.id, rec.designs[0].id, _spec())
    missing = client.post(f"/api/orders/{order.id}/request", json={"name": "A", "email": "a@example.com"})
    assert missing.status_code == 422
    blank = client.post(f"/api/orders/{order.id}/request", json={"name": "   ", "email": "a@example.com", "address": "x"})
    assert blank.status_code == 422


def test_print_svg_resanitizes_stored_design():
    evil = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000" onload="alert(1)">'
        '<script>alert(1)</script><rect width="10" height="10" fill="#ff0000"/></svg>'
    )
    rec = seed_session(done=True, profile=True)
    live = store.require_session(rec.id)
    live.designs = [make_design(svg=evil)]  # simulate a design stored before sanitizing existed
    store.save_session(live)
    order = orders.create_order(rec.id, live.designs[0].id, _spec())
    out = orders.print_svg(order)
    assert "script" not in out and "onload" not in out and "<rect" in out


def test_print_svg_unusable_design_is_conflict(client):
    from app.errors import ConflictError

    rec = seed_session(done=True, profile=True)
    live = store.require_session(rec.id)
    live.designs = [make_design(svg="<html>not an svg</html>")]
    store.save_session(live)
    order = orders.create_order(rec.id, live.designs[0].id, _spec())
    with pytest.raises(ConflictError):
        orders.print_svg(order)
    assert client.get(f"/api/orders/{order.id}/print.svg").status_code == 409
