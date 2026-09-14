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
    ok: bool
    detail: str
    merchant_order_id: str | None = None


class PaymentRail(ABC):
    rail_id: str

    @abstractmethod
    def pay(self, *, payment_mandate: dict[str, Any], cart_ref: str, adapter: Any) -> PayResult: ...
