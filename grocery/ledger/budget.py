"""
The hard caps. Checked by the broker before every execute() — never by the
model, never in a prompt. This module is what makes "overspending is
structurally impossible" a true sentence rather than an aspiration.
"""
from __future__ import annotations

from dataclasses import dataclass

from grocery import config
from grocery.ledger import store


@dataclass
class BudgetCheck:
    allowed: bool
    reason: str
    remaining_today_inr: float
    remaining_month_inr: float


def check(amount_inr: float) -> BudgetCheck:
    if amount_inr > config.CAP_PER_ORDER_INR:
        return BudgetCheck(
            False,
            f"₹{amount_inr:.2f} exceeds the per-order cap of ₹{config.CAP_PER_ORDER_INR}",
            remaining_today_inr=config.CAP_PER_DAY_INR - store.spend_today(),
            remaining_month_inr=config.CAP_PER_MONTH_INR - store.spend_this_month(),
        )

    today_after = store.spend_today() + amount_inr
    if today_after > config.CAP_PER_DAY_INR:
        remaining = config.CAP_PER_DAY_INR - store.spend_today()
        return BudgetCheck(
            False,
            f"₹{amount_inr:.2f} would exceed today's remaining budget of ₹{remaining:.2f} "
            f"(daily cap ₹{config.CAP_PER_DAY_INR})",
            remaining_today_inr=remaining,
            remaining_month_inr=config.CAP_PER_MONTH_INR - store.spend_this_month(),
        )

    month_after = store.spend_this_month() + amount_inr
    if month_after > config.CAP_PER_MONTH_INR:
        remaining = config.CAP_PER_MONTH_INR - store.spend_this_month()
        return BudgetCheck(
            False,
            f"₹{amount_inr:.2f} would exceed this month's remaining budget of ₹{remaining:.2f} "
            f"(monthly cap ₹{config.CAP_PER_MONTH_INR})",
            remaining_today_inr=config.CAP_PER_DAY_INR - store.spend_today(),
            remaining_month_inr=remaining,
        )

    return BudgetCheck(
        True, "within caps",
        remaining_today_inr=config.CAP_PER_DAY_INR - today_after,
        remaining_month_inr=config.CAP_PER_MONTH_INR - month_after,
    )
