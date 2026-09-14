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


def rank_by_landed_cost(breakdowns: list[LandedCostBreakdown]) -> list[RankedCart]:
    """Every merchant that produced a breakdown appears in the output — a
    merchant below its own minimum order is still shown, ranked after every
    eligible one, with below_min_order visible, rather than silently
    disappearing from the comparison. Hiding it would make "only one
    merchant shown" look like "only one merchant available", which is a
    different and false claim (see tests/e2e — a single-item order can
    legitimately clear only one merchant's minimum)."""
    eligible = [b for b in breakdowns if not b.below_min_order]
    ineligible = [b for b in breakdowns if b.below_min_order]

    cheapest_items = min(breakdowns, key=lambda b: b.items_subtotal_inr) if breakdowns else None
    cheapest_landed = min(eligible, key=lambda b: b.landed_total_inr) if eligible else None

    ordered = sorted(eligible, key=lambda b: b.landed_total_inr) + sorted(ineligible, key=lambda b: b.landed_total_inr)
    out = []
    for i, b in enumerate(ordered, start=1):
        out.append(RankedCart(
            breakdown=b, rank=i,
            cheapest_by_items_only=(b is cheapest_items),
            cheapest_by_landed_cost=(b is cheapest_landed),
        ))
    return out
