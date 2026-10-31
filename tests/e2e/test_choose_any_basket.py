"""The human can take any compared basket, not only the recommended one.

Gourab: "out of the 3 baskets i should be able to proceed with anyone, not just
what the agent recommends." The ranking rule knows fulfilment and price; it does
not know which shop he trusts or which one is quick to his door today.

The property that makes this safe is that NO NEW PRICE IS CREATED — every basket
was assembled by the worker from live listings and is already in
order.comparison. These tests pin exactly that.
"""
from __future__ import annotations

import httpx
import pytest


def _proposed_order_with_two_baskets(live):
    """A real order with a real comparison, via the real worker planner."""
    client = httpx.Client(base_url=live["console"], timeout=30)
    client.post("/auth/setup", data={"passphrase_value": "correct horse battery staple"})
    # "atta" rather than "milk": a single milk clears only dailygrocer's minimum
    # order, so there is no second basket to switch TO and every interesting
    # assertion skips. Atta is dear enough to clear all three.
    items = ["atta", "biscuits"]
    order_id = client.post("/api/intent",
                           json={"items": items, "max_total_inr": 800}).json()["order_id"]
    import worker.run as worker_run
    worker_run._plan_and_propose(order_id, items, 800)
    order = client.get(f"/api/order/{order_id}").json()
    assert order["state"] == "proposed"
    return client, order_id, order


def test_a_human_can_switch_to_another_store(live):
    client, order_id, order = _proposed_order_with_two_baskets(live)
    recommended = order["cart"]["merchant"]
    others = [c["merchant"] for c in order["comparison"]
              if c["merchant"] != recommended and c.get("line_items")
              and not c.get("below_min_order")]
    if not others:
        pytest.skip("this basket had only one viable store")

    target = others[0]
    r = client.post(f"/console/order/{order_id}/choose_basket", data={"merchant": target})
    assert r.status_code == 200, r.text

    after = client.get(f"/api/order/{order_id}").json()
    assert after["state"] == "proposed"
    assert after["cart"]["merchant"] == target
    assert after["cart_version"] > order["cart_version"]
    assert after["cart"]["signature"], "a human-chosen cart is signed like any other"


def test_switching_invents_no_price(live):
    """The decisive property. Every line and the total must be exactly what the
    worker already published for that store."""
    client, order_id, order = _proposed_order_with_two_baskets(live)
    recommended = order["cart"]["merchant"]
    others = [c for c in order["comparison"]
              if c["merchant"] != recommended and c.get("line_items")
              and not c.get("below_min_order")]
    if not others:
        pytest.skip("this basket had only one viable store")

    entry = others[0]
    client.post(f"/console/order/{order_id}/choose_basket", data={"merchant": entry["merchant"]})
    cart = client.get(f"/api/order/{order_id}").json()["cart"]

    assert cart["landed_total_inr"] == entry["landed_total_inr"]
    assert [l["sku"] for l in cart["line_items"]] == [l["sku"] for l in entry["line_items"]]
    for line in cart["line_items"]:
        assert line["price_source"], "every line keeps the provenance the worker gave it"


def test_an_unknown_store_is_refused(live):
    client, order_id, _ = _proposed_order_with_two_baskets(live)
    r = client.post(f"/console/order/{order_id}/choose_basket", data={"merchant": "tesco"})
    assert r.status_code == 409
    assert "no basket from" in r.text


def test_a_basket_over_the_cap_is_refused(live):
    """A new cart mandate is checked on its own terms, whatever was true when
    the comparison was built."""
    from console import approval, choose_basket

    client, order_id, _ = _proposed_order_with_two_baskets(live)
    order = approval.load(order_id)
    order.comparison = [{"merchant": "expensive", "landed_total_inr": 999999.0,
                         "line_items": [{"sku": "x", "name": "Gold", "qty": 1,
                                         "line_total_inr": 999999.0}]}]
    approval._save(order)

    with pytest.raises(choose_basket.BasketUnavailable, match="over the"):
        choose_basket.build_cart(approval.load(order_id), "expensive", cart_version=2)


def test_switching_is_recorded_as_the_humans_decision(live):
    """Who chose this cart is a fact the audit chain should carry."""
    client, order_id, order = _proposed_order_with_two_baskets(live)
    others = [c["merchant"] for c in order["comparison"]
              if c["merchant"] != order["cart"]["merchant"] and c.get("line_items")
              and not c.get("below_min_order")]
    if not others:
        pytest.skip("this basket had only one viable store")

    client.post(f"/console/order/{order_id}/choose_basket", data={"merchant": others[0]})

    from grocery.audit import decisions
    actions = [r for r in decisions.read_all()
               if r.get("trace_id") == order_id and r.get("action") == "basket_chosen_by_human"]
    assert actions, "a human overriding the ranking must be visible in the trace"
    assert others[0] in actions[-1]["reasoning"]
