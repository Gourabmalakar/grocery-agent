"""
Ranks merchants by landed cost. This is the function whose output the
Console's "Compare" screen renders and whose output cart-auditor
independently recomputes rather than trusts.
"""
from __future__ import annotations

from dataclasses import dataclass

from grocery.compare.landed_cost import LandedCostBreakdown


@dataclass
class RankedCart:
    breakdown: LandedCostBreakdown
    rank: int
    cheapest_by_items_only: bool  # True if this merchant would win on subtotal alone
    cheapest_by_landed_cost: bool


def rank_by_landed_cost(breakdowns: list[LandedCostBreakdown],
                        *, fulfilment: dict[str, int] | None = None) -> list[RankedCart]:
    """Ranks merchants — BY HOW MUCH OF THE ASK THEY FILL FIRST, then by cost.

    `fulfilment` maps merchant -> how many of the requested items it could
    actually supply. Passing it is what makes the comparison mean anything, and
    omitting it keeps the old pure-price behaviour for callers that have no such
    notion (the mock path, and every existing test).

    WHY IT IS NOT PRICE ALONE. Cheapest-wins silently assumes the carts hold the
    same things. In order_6a45f91aaa2e they did not: on one revision Blinkit
    "won" at ₹158 having found two of the five items, against carts that found
    all five, and the system recommended it without a word. That is not a cheaper
    way to buy the groceries; it is a smaller number attached to less shopping.
    Two of five at ₹158 and five of five at ₹631 are not comparable quantities,
    and picking the smaller one is a category error, not a bargain.

    A merchant that fills fewer items still APPEARS, ranked below, with its
    counts visible — same principle as below_min_order below.

    Every merchant that produced a breakdown appears in the output — a
    merchant below its own minimum order is still shown, ranked after every
    eligible one, with below_min_order visible, rather than silently
    disappearing from the comparison. Hiding it would make "only one
    merchant shown" look like "only one merchant available", which is a
    different and false claim (see tests/e2e — a single-item order can
    legitimately clear only one merchant's minimum)."""
    eligible = [b for b in breakdowns if not b.below_min_order]
    ineligible = [b for b in breakdowns if b.below_min_order]

    fulfilment = fulfilment or {}
    # Negated so that more-fulfilled sorts first while cheaper still sorts first
    # within an equal fulfilment. With no fulfilment map every merchant scores 0
    # and the ordering collapses to pure price, exactly as before.
    def key(b: LandedCostBreakdown) -> tuple[int, float]:
        return (-fulfilment.get(b.merchant, 0), b.landed_total_inr)

    # "Cheapest" is only claimed among merchants that fill the MOST of the ask.
    # Calling a two-of-five cart the cheapest is the sentence that made the
    # narrative lie, so the title is not available to it.
    best_fulfilment = max((fulfilment.get(b.merchant, 0) for b in eligible), default=0)
    contenders = [b for b in eligible if fulfilment.get(b.merchant, 0) == best_fulfilment]

    cheapest_items = min(contenders, key=lambda b: b.items_subtotal_inr) if contenders else None
    cheapest_landed = min(contenders, key=lambda b: b.landed_total_inr) if contenders else None

    ordered = sorted(eligible, key=key) + sorted(ineligible, key=key)
    out = []
    for i, b in enumerate(ordered, start=1):
        out.append(RankedCart(
            breakdown=b, rank=i,
            cheapest_by_items_only=(b is cheapest_items),
            cheapest_by_landed_cost=(b is cheapest_landed),
        ))
    return out
