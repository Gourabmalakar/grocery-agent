"""Rail 3: the agent places the order and Gourab pays the rider at the door.

THE ONLY FULLY HANDS-OFF PATH THAT WORKS TODAY, and the one the plan always
said should come first. No payment instrument is touched, nothing is drainable,
and the blast radius is a single order that can be refused at the door. Swiggy
needs to enable nothing for it — unlike SwiggyPay, which waits on
agenticPaymentEligible.

IT STILL COUNTS AS SPEND. No money moves at checkout, but a real order is
placed and a real debt is incurred, so it records a spend and consumes the caps
exactly as the wallet rail does. Treating COD as free because the card was not
charged is how a ₹500 daily cap becomes unlimited.

WHAT IT CANNOT UNDO. `checkout` is irreversible here as everywhere — there is
no cancel tool on this surface. The refusal to retry on an uncertain response
matters more for COD than for the wallet, because a duplicate COD order is two
riders arriving with two bags.
"""
from __future__ import annotations

from typing import Any

from console.rails.base import PaymentRail, PayResult
from grocery.carts import swiggy as swiggy_cart
from grocery.carts import swiggy_checkout

_TOLERANCE_INR = 0.01


class SwiggyCodRail(PaymentRail):
    rail_id = "swiggy_cod"

    def __init__(self, *, token_provider, address_id: str) -> None:
        self._token_provider = token_provider
        self._address_id = address_id

    def pay(self, *, payment_mandate: dict[str, Any], cart_ref: str,
            adapter: Any) -> PayResult:
        approved = float(payment_mandate.get("amount_inr") or 0)
        if approved <= 0:
            return PayResult(ok=False, settled=False,
                             detail="no approved amount on the payment mandate")

        cart_client = swiggy_cart.SwiggyCartClient(token_provider=self._token_provider)
        try:
            totals = swiggy_cart.totals_of(cart_client.fetch())
        except Exception as exc:  # noqa: BLE001
            return PayResult(ok=False, settled=False,
                             detail=f"could not read the Swiggy cart: {str(exc)[:200]}")
        if not totals.found:
            return PayResult(ok=False, settled=False,
                             detail="Swiggy did not report a cart total, so there "
                                    "is no number to check the approval against")

        # checkout() commits whatever is in the cart at that instant, so this
        # read is the last gate between an approved total and a different one.
        if abs(totals.total_inr - approved) > _TOLERANCE_INR:
            return PayResult(
                ok=False, settled=False,
                detail=(f"Swiggy's cart is ₹{totals.total_inr:.2f} but ₹{approved:.2f} "
                        f"was approved — refusing to order a total you did not see"))

        checkout = swiggy_checkout.SwiggyCheckoutClient(token_provider=self._token_provider)
        try:
            checkout.assert_can_pay_cod()
        except swiggy_checkout.CheckoutRefused as exc:
            return PayResult(ok=False, settled=False, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            return PayResult(ok=False, settled=False,
                             detail=f"could not check Swiggy's payment options: {str(exc)[:200]}")

        # No retries past this line. A duplicate COD order is two riders.
        try:
            response = checkout.place_order(address_id=self._address_id,
                                            payment_method=swiggy_checkout.COD)
        except swiggy_checkout.CheckoutRefused as exc:
            return PayResult(ok=False, settled=False,
                             detail=f"Swiggy declined to place the order: {exc}")

        merchant_order_id = swiggy_checkout.order_id_of(response)
        if not merchant_order_id:
            raise swiggy_checkout.CheckoutUncertain(
                "Swiggy accepted the order but returned no order id")
        return PayResult(ok=True, settled=True, merchant_order_id=merchant_order_id,
                         detail=f"order placed, ₹{approved:.2f} payable to the rider")
