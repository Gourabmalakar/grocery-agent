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
        """RETURNS A FAILURE; it must not raise.

        It used to raise NotImplementedError, which propagated out of
        execute() AFTER the order had been transitioned to EXECUTING — so the
        order was stranded in a state with no recovery path, looking exactly
        like an order that might have been paid. A rail that cannot pay has to
        say so in the one vocabulary execute() handles, which is PayResult.

        The real autonomous rail is console/rails/swiggy_wallet.py. This stays
        as the generic placeholder so a misconfiguration fails closed and
        legibly rather than by exception.
        """
        return PayResult(
            ok=False,
            detail="the generic merchant_wallet rail is not implemented — "
                   "configure a merchant-specific rail instead",
            settled=False,
        )
