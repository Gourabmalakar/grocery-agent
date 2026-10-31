"""A real merchant's prices can be read; no order can be placed with it.

Before console/adapters.py, `console_approve_payment` did
`MockAdapter(order.cart["merchant"])` unconditionally. On a real JioMart cart
that built a mock adapter pointed at a merchant mockstore has never heard of —
and the danger was not a crash but a plausible run that could mint a
PaymentMandate and write a merchant order id for an order no merchant received.
An audit chain that records something which did not happen is worse than one
that records nothing.
"""
from __future__ import annotations

import pytest

REAL_MERCHANTS = ["jiomart", "blinkit", "zepto", "swiggy"]
MOCK_MERCHANTS = ["freshmart", "quickbasket", "dailygrocer"]


@pytest.mark.parametrize("merchant", REAL_MERCHANTS)
def test_execution_is_refused_for_every_real_merchant(isolated_data_dir, merchant):
    from console import adapters

    with pytest.raises(adapters.ExecutionUnavailable, match="cannot place an order"):
        adapters.adapter_for(merchant, for_execution=True)


@pytest.mark.parametrize("merchant", MOCK_MERCHANTS)
def test_execution_is_allowed_for_the_mock_merchants(isolated_data_dir, merchant):
    """The payment path must stay testable — that is the whole reason mockstore
    survives as a test double."""
    from console import adapters

    assert adapters.adapter_for(merchant, for_execution=True) is not None


def test_the_allowlist_is_positive(isolated_data_dir):
    """A merchant added next month is non-executable by default."""
    from console import adapters

    with pytest.raises(adapters.ExecutionUnavailable):
        adapters.adapter_for("some_merchant_added_next_month", for_execution=True)


def test_reading_is_not_gated(isolated_data_dir):
    """Refusing to ORDER is the point; refusing to read would break the
    comparison this whole stage exists to produce."""
    from console import adapters

    assert adapters.adapter_for("jiomart", for_execution=False) is not None


def test_the_refusal_names_the_merchant_and_says_nothing_was_charged(isolated_data_dir):
    from console import adapters

    with pytest.raises(adapters.ExecutionUnavailable) as exc:
        adapters.adapter_for("swiggy", for_execution=True)
    message = str(exc.value)
    assert "swiggy" in message
    assert "nothing has been charged" in message.lower()


def test_the_payment_route_409s_and_leaves_the_order_approved(live):
    """Through the real HTTP boundary: the order must stay `approved`, not be
    burned. Whether to abort a cart the human just approved is their decision."""
    import httpx
    from grocery.mandates import intent as intent_m, cart as cart_m
    from console import approval

    client = httpx.Client(base_url=live["console"], timeout=10)
    r = client.post("/auth/setup", data={"passphrase_value": "correct horse battery staple"})
    assert r.status_code == 200, r.text

    # An order carrying a REAL merchant's cart, minted the same way the worker does.
    intent = intent_m.new_intent(items=["milk"], max_total_inr=500)
    intent_m.persist(intent)
    order = approval.create_order(intent)
    cart = cart_m.new_cart(
        intent_id=intent["intent_id"], merchant="jiomart",
        line_items=[{"sku": "jm-milk-1l", "qty": 1, "intent_ref": intent["intent_id"],
                     "price_source": "jiomart_api", "asof": 0, "price_inr": 69.0}],
        fees={"delivery": 0, "handling": 0, "surge": 0, "coupon": 0},
        landed_total_inr=69.0,
    )
    cart_m.persist(cart)
    order = approval.propose_cart(order.order_id, cart)
    order = approval.approve_cart(order.order_id, approved_by="gourab",
                                  approval_factor="passphrase")
    assert order.state == approval.State.APPROVED

    r = client.post(f"/console/order/{order.order_id}/approve_payment")
    assert r.status_code == 409, r.text
    assert "cannot place an order" in r.text

    still = client.get(f"/api/order/{order.order_id}").json()
    assert still["state"] == "approved", "the order must not be burned by hitting the boundary"
    assert not still.get("payment"), "no payment mandate may exist"


def test_the_page_offers_no_payment_button_for_a_real_merchant(live):
    """A button that 409s teaches the human the system is flaky."""
    import httpx
    from grocery.mandates import intent as intent_m, cart as cart_m
    from console import approval

    client = httpx.Client(base_url=live["console"], timeout=10)
    client.post("/auth/setup", data={"passphrase_value": "correct horse battery staple"})

    intent = intent_m.new_intent(items=["milk"], max_total_inr=500)
    intent_m.persist(intent)
    order = approval.create_order(intent)
    cart = cart_m.new_cart(
        intent_id=intent["intent_id"], merchant="blinkit",
        line_items=[{"sku": "19512", "qty": 1, "intent_ref": intent["intent_id"],
                     "price_source": "blinkit_live", "asof": 0, "price_inr": 29.0}],
        fees={"delivery": 0, "handling": 0, "surge": 0, "coupon": 0},
        landed_total_inr=29.0,
    )
    cart_m.persist(cart)
    order = approval.propose_cart(order.order_id, cart)
    approval.approve_cart(order.order_id, approved_by="gourab", approval_factor="passphrase")

    page = client.get(f"/orders/{order.order_id}").text
    assert "Approve payment" not in page
    assert "is a real merchant" in page
    assert "Abort" in page, "the human still needs a way out"
