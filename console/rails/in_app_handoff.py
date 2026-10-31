"""Rail 2 of 2: the basket is built and the human pays in the merchant's app.

NOTHING IS CHARGED HERE, and the whole point of this rail is to say so
precisely. It returns ok=True (the rail did exactly what it is for) with
settled=False (no money moved), so execute() parks the order AWAITING PAYMENT
instead of marking it DONE and the ledger records a hand-off instead of a
spend. The caps are untouched, because nothing was spent.

This is the safer of the two rails by a wide margin and it is the default. The
cart is already in his real Swiggy account — same cartId his phone shows — so
"pay in the app" is one tap for him and zero payment authority for this system.
It is also the fallback whenever the wallet rail is refused, which on this
account it currently is: Swiggy reports agenticPaymentEligible=false.
"""
from __future__ import annotations

from typing import Any

from console.rails.base import PaymentRail, PayResult


class InAppHandoffRail(PaymentRail):
    rail_id = "in_app_handoff"

    def pay(self, *, payment_mandate: dict[str, Any], cart_ref: str,
            adapter: Any) -> PayResult:
        amount = float(payment_mandate.get("amount_inr") or 0)
        return PayResult(
            ok=True,
            settled=False,
            merchant_order_id=None,
            detail=(f"basket of ₹{amount:.2f} is waiting in your Swiggy app — "
                    f"nothing has been charged"),
        )
