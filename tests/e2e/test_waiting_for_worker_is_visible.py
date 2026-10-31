"""An order waiting on a worker must say so.

order_b837482ed7ee sat in `revising` with a null cart because the worker process
had already exited when the revision was typed. The page rendered no cart card
(it requires o.cart) and auto-refreshed forever: a spinner with no information
and no forward path, which is the same dead end the repo already fixed once on
the no-viable-cart route. The revision was never lost — restarting the worker
picked it straight up — but nothing on screen said that.
"""
from __future__ import annotations

import httpx


def _login(base):
    c = httpx.Client(base_url=base, timeout=10)
    c.post("/auth/setup", data={"passphrase_value": "correct horse battery staple"})
    return c


def test_a_draft_order_tells_you_to_start_the_worker(live):
    client = _login(live["console"])
    order_id = client.post("/api/intent", json={"items": ["milk"], "max_total_inr": 800}).json()["order_id"]

    page = client.get(f"/orders/{order_id}").text
    assert "worker.run" in page, "the page must name the command that moves this forward"
    assert order_id in page, "the command must carry this order's id, ready to copy"
    assert "separate process" in page


def test_a_revising_order_says_the_revision_is_waiting_not_lost(live):
    """The exact shape of the reported bug: revising, no cart, no worker."""
    from console import approval
    from grocery.mandates import cart as cart_m, intent as intent_m

    client = _login(live["console"])
    intent = intent_m.new_intent(items=["milk"], max_total_inr=800)
    intent_m.persist(intent)
    order = approval.create_order(intent)
    cart = cart_m.new_cart(
        intent_id=intent["intent_id"], merchant="dailygrocer",
        line_items=[{"sku": "dg-milk-1l", "qty": 1, "intent_ref": intent["intent_id"],
                     "price_source": "test", "asof": 0, "price_inr": 50.0}],
        fees={"delivery": 0, "handling": 0, "surge": 0, "coupon": 0}, landed_total_inr=50.0,
    )
    cart_m.persist(cart)
    approval.propose_cart(order.order_id, cart)
    approval.request_revision(order.order_id, instruction_text="make it 2 litres")

    fresh = approval.load(order.order_id)
    assert fresh.state == approval.State.REVISING and fresh.cart is None

    page = client.get(f"/orders/{order.order_id}").text
    assert "waiting to be picked up" in page
    assert "worker.run" in page and order.order_id in page
    assert "Abort" in page, "the human needs a way out that is not closing the tab"
