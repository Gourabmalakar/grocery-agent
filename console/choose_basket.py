"""Letting the human take a basket the agent did not recommend.

Gourab: "out of the 3 baskets i should be able to proceed with anyone, not just
what the agent recommends." He is right, and the reason is not convenience. The
agent ranks on fulfilment and then price, which is a defensible rule and still
only a rule — it knows nothing about which shop he trusts, which one delivers to
him fastest today, or that he would rather pay ₹40 more than wait. A comparison
that can only be accepted whole is a recommendation wearing the costume of a
choice.

WHAT THIS DOES NOT DO, and the distinction matters. It mints no price, searches
nothing, and invents no line. Every basket here was assembled by the WORKER in
the round that produced the current proposal, from live listings with their own
price_source and asof, and is sitting in order.comparison. Choosing one re-mints
that same set of lines as a signed CartMandate at the next cart_version. The
authority added is zero: the human is picking between things the worker already
proposed, which is exactly the authority a human already had when they pressed
Approve on the first one.

IT RUNS IN THE CONSOLE because the alternative is worse. Routing it through the
worker would mean a basket cannot be switched unless a worker process happens to
be alive — and an order stuck because nothing was listening is the failure
Gourab hit one revision earlier.

The caps are re-checked here regardless of what was checked when the comparison
was built, because this produces a NEW cart mandate and every cart mandate is
checked on its own terms.
"""
from __future__ import annotations

from typing import Any

from grocery import config
from grocery.mandates import cart as cart_mandate


class BasketUnavailable(RuntimeError):
    """This basket cannot become the cart, and the message says why."""


def basket_for(order, merchant: str) -> dict[str, Any]:
    for entry in (order.comparison or []):
        if entry.get("merchant") == merchant:
            return entry
    raise BasketUnavailable(
        f"there is no basket from {merchant!r} in this comparison — the stores offered are "
        f"{[e.get('merchant') for e in (order.comparison or [])]}"
    )


def build_cart(order, merchant: str, *, cart_version: int) -> dict[str, Any]:
    """Re-mint one of the compared baskets as a signed cart mandate."""
    entry = basket_for(order, merchant)
    lines = entry.get("line_items") or []
    if not lines:
        raise BasketUnavailable(
            f"{merchant} found nothing for this order, so there is no basket to take"
        )
    if entry.get("below_min_order"):
        raise BasketUnavailable(
            f"{merchant}'s basket is below its own minimum order, so it cannot be bought as it "
            "stands — add to it, or choose another store"
        )

    total = float(entry.get("landed_total_inr") or 0)
    cap = order.intent["max_total_inr"]
    if total > cap:
        raise BasketUnavailable(
            f"{merchant}'s basket is ₹{total:.2f}, over the ₹{cap} you set for this order. "
            "Raising that takes a fresh request, not a revision."
        )
    if total > config.CAP_PER_ORDER_INR:
        raise BasketUnavailable(
            f"{merchant}'s basket is ₹{total:.2f}, over the ₹{config.CAP_PER_ORDER_INR} "
            "per-order limit this system holds itself to."
        )

    # The amendment this cart traces to is whatever the current cart traced to:
    # choosing a different store is not a change of what was asked for, so the
    # lineage check in propose_cart still has to pass on the same reference.
    amendment_ids = [a["amendment_id"] for a in (order.amendments or [])]
    return cart_mandate.new_cart(
        intent_id=order.intent["intent_id"], merchant=merchant,
        line_items=[dict(line) for line in lines],
        fees={"delivery": float(entry.get("delivery_fee_inr") or 0),
              "handling": float(entry.get("handling_fee_inr") or 0),
              "surge": 0, "coupon": 0},
        landed_total_inr=total, cart_version=cart_version,
        amendment_ids=amendment_ids,
    )
