"""
Phase 4 rail: replays a merchant session token (or Swiggy OAuth token) held
in the encrypted vault. NOT IMPLEMENTED in Phase 0-3 — deliberately raises,
so MODE=live cannot silently fall through to a rail nobody has built or
tested yet.
"""
from __future__ import annotations

from typing import Any

from console.rails.base import PaymentRail, PayResult


class WalletRail(PaymentRail):
    rail_id = "merchant_wallet"

    def pay(self, *, payment_mandate: dict[str, Any], cart_ref: str, adapter: Any) -> PayResult:
        raise NotImplementedError(
            "merchant_wallet rail is a Phase 4 deliverable — see the plan's "
            "phasing section. It is not wired up, and MODE=live must not "
            "silently fall back to it."
        )
