"""
Landed cost — items + delivery + handling + surge − coupons — which is what
actually decides the cheapest cart. The headline per-item price ranking
routinely inverts once delivery fees and minimum-order thresholds are
accounted for (see tests/e2e scenario 2, and mockstore/catalog.py's
QuickBasket fixture, which is deliberately cheap per-item but has a high
free-delivery threshold).
"""
from __future__ import annotations

from dataclasses import dataclass

from grocery.adapters.base import Quote


@dataclass
class LandedCostBreakdown:
    merchant: str
    items_subtotal_inr: float
    delivery_fee_inr: float
    handling_fee_inr: float
    coupon_discount_inr: float
    landed_total_inr: float
    below_min_order: bool
    min_order_shortfall_inr: float
    fees_known: bool = True


def compute(quote: Quote) -> LandedCostBreakdown:
    return LandedCostBreakdown(
        merchant=quote.merchant,
        items_subtotal_inr=quote.subtotal_inr,
        delivery_fee_inr=quote.delivery_fee_inr,
        handling_fee_inr=quote.handling_fee_inr,
        coupon_discount_inr=quote.coupon_discount_inr,
        landed_total_inr=quote.landed_total_inr,
        below_min_order=quote.below_min_order,
        min_order_shortfall_inr=quote.min_order_shortfall_inr,
    )


def suggest_min_order_topup(quote: Quote, next_free_delivery_threshold_inr: float | None) -> str | None:
    """"Add ₹120 to avoid a ₹40 delivery fee" — arithmetically grounded advice,
    not a generic nudge. Returns None when there's nothing useful to suggest.

    Deliberately does NOT gate on shortfall being smaller than the fee saved —
    whether ₹120 more is worth ₹40 off is the human's call, not this
    function's; the function's job is only to make sure the number offered
    is real."""
    if next_free_delivery_threshold_inr is None:
        return None
    shortfall = next_free_delivery_threshold_inr - quote.subtotal_inr
    if shortfall <= 0 or quote.delivery_fee_inr <= 0:
        return None
    return f"Add ₹{shortfall:.0f} more to clear free delivery and save ₹{quote.delivery_fee_inr:.0f}"


def compute_subtotal_only(merchant: str, subtotal_inr: float) -> LandedCostBreakdown:
    """For real merchants where delivery/handling fees can't be read without
    building a cart. Ranks on item subtotal only; the narrative must disclose
    that fees are not included."""
    return LandedCostBreakdown(
        merchant=merchant, items_subtotal_inr=subtotal_inr,
        delivery_fee_inr=0.0, handling_fee_inr=0.0,
        coupon_discount_inr=0.0, landed_total_inr=subtotal_inr,
        below_min_order=False, min_order_shortfall_inr=0.0,
        fees_known=False,
    )


def compute_from_subtotal(merchant: str, subtotal_inr: float, meta: dict) -> LandedCostBreakdown:
    """Same computation mockstore itself does internally, duplicated here so
    the worker can rank merchants from read-only /search + /merchants data
    without ever calling a write endpoint (build_cart/place_order). Kept in
    lockstep with mockstore/app.py's build_cart logic by tests/adapters/,
    which asserts the two never disagree."""
    below_min = subtotal_inr < meta["min_order_inr"]
    delivery_fee = 0.0 if subtotal_inr >= meta["free_delivery_over_inr"] else meta["delivery_fee_inr"]
    handling_fee = meta["handling_fee_inr"]
    landed_total = subtotal_inr + delivery_fee + handling_fee
    shortfall = max(0.0, meta["min_order_inr"] - subtotal_inr)
    return LandedCostBreakdown(
        merchant=merchant, items_subtotal_inr=subtotal_inr,
        delivery_fee_inr=delivery_fee, handling_fee_inr=handling_fee,
        coupon_discount_inr=0.0, landed_total_inr=landed_total,
        below_min_order=below_min, min_order_shortfall_inr=shortfall,
    )
