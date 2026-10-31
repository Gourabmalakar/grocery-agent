"""The object execute() re-quotes against when the merchant is Swiggy.

execute() takes an `adapter` and calls build_cart() then quote() on it at the
last moment before paying. For the mock merchant that genuinely builds a cart.
For Swiggy it must NOT: the basket is already in his account — built earlier,
seen by him, and the thing he approved. Rebuilding it here would replace the
cart he looked at with a fresh one (Swiggy's update_cart has no add, only
replace) moments before charging for it.

So build_cart() returns the existing reference and quote() READS Swiggy's own
cart. That read is the real price-drift check and it is a better one than the
mock path gets, because it is the merchant's authoritative bill — item total,
handling, delivery and GST — rather than a recomputation of ours.
"""
from __future__ import annotations

from typing import Any

from grocery.adapters.base import Quote
from grocery.carts import swiggy as swiggy_cart


class SwiggyExecutionAdapter:
    merchant_id = "swiggy"

    def __init__(self, *, token_provider, cart_ref: str) -> None:
        self._client = swiggy_cart.SwiggyCartClient(token_provider=token_provider)
        self._cart_ref = cart_ref

    def build_cart(self, lines: Any, *, authorized: bool = False) -> str:
        """Returns the basket that is already there. Deliberately does not write.

        `lines` is accepted and ignored because execute() passes it; ignoring it
        is correct here and silence would not be, hence this docstring. The cart
        was built, verified line by line and shown to him; the only thing left
        to do with it is read it.
        """
        return self._cart_ref

    def quote(self, cart_ref: str) -> Quote:
        totals = swiggy_cart.totals_of(self._client.fetch())
        if not totals.found:
            raise RuntimeError(
                "Swiggy did not report a cart total at payment time, so there is "
                "no number to check the approval against")
        return Quote(
            merchant="swiggy",
            subtotal_inr=totals.subtotal_inr,
            delivery_fee_inr=totals.fees_inr,
            handling_fee_inr=0.0,
            coupon_discount_inr=0.0,
            landed_total_inr=totals.total_inr,
        )

    def place_order(self, *args: Any, **kwargs: Any) -> str:
        """Never. Paying is the rail's job, behind a mandate and a passphrase —
        not something an adapter handed to execute() can do on its own."""
        raise NotImplementedError(
            "SwiggyExecutionAdapter cannot place orders; the rail does that")
