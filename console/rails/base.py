"""
Payment rail contract. Every rail exposes the same shape so the broker's
execute() path is identical regardless of which rail a given order uses.

A rail's `pay()` is called ONLY by console/approval.py's execute(), which
itself is called only after a PaymentMandate has been minted — never
reachable from the worker process directly. See worker/tools.py, which does
not expose any rail.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class PayResult:
    """What a rail did.

    `ok` and `settled` are DIFFERENT QUESTIONS and conflating them is how a
    hand-off gets recorded as a payment:

      ok=True,  settled=True   money moved. The order is DONE and the amount
                               counts against the caps.
      ok=True,  settled=False  the rail did exactly what it is for and charged
                               nothing — a basket handed to a human to pay in
                               the merchant's own app. The order is AWAITING
                               PAYMENT, not DONE, and nothing counts against
                               the caps because nothing was spent.
      ok=False                 it failed. The order is REFUSED.

    UpiHandoffRail used to return ok=True having paid nothing, with no way to
    express the difference, so the order went straight to DONE and the ledger
    recorded a spend that had not happened.
    """
    ok: bool
    detail: str
    merchant_order_id: str | None = None
    settled: bool = True


class PaymentRail(ABC):
    rail_id: str

    @abstractmethod
    def pay(self, *, payment_mandate: dict[str, Any], cart_ref: str, adapter: Any) -> PayResult: ...
