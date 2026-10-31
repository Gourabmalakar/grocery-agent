"""Rail 1 of 2: the agent pays, autonomously, from the Swiggy Money wallet.

WHY THE WALLET AND NOTHING ELSE. The float in Swiggy Money IS the blast radius.
A compromised token, a bug in the ranking, a basket the model got wrong — all of
them stop at whatever was manually put in that wallet. UPI and cards reach a
bank account with no such ceiling, so this rail refuses them even though Swiggy
offers them (see grocery/carts/swiggy_checkout.py). NEVER add auto-top-up: the
moment the float refills itself, the ceiling is gone and so is the argument for
this design.

WHAT IT CHECKS BEFORE SPENDING. execute() has already verified the cart hash,
re-quoted, checked the caps and minted a signed PaymentMandate. This rail adds
the last one that can only be done here: it reads Swiggy's OWN cart total
immediately before calling checkout, and refuses if it differs from the approved
amount by more than a paisa. `checkout` takes no cart argument and no amount —
it charges whatever is in the cart at that instant — so this read is the only
thing standing between an approved ₹451 and a cart that changed underneath it.
"""
from __future__ import annotations

from typing import Any

from console.rails.base import PaymentRail, PayResult
from grocery.carts import swiggy as swiggy_cart
from grocery.carts import swiggy_checkout

_TOLERANCE_INR = 0.01


class SwiggyWalletRail(PaymentRail):
    rail_id = "swiggy_wallet"

    def __init__(self, *, token_provider, address_id: str) -> None:
        self._token_provider = token_provider
        self._address_id = address_id

    def pay(self, *, payment_mandate: dict[str, Any], cart_ref: str,
            adapter: Any) -> PayResult:
        approved = float(payment_mandate.get("amount_inr") or 0)
        if approved <= 0:
            return PayResult(ok=False, settled=False,
                             detail="no approved amount on the payment mandate")

        # 1. Read the merchant's own cart, now.
        cart_client = swiggy_cart.SwiggyCartClient(token_provider=self._token_provider)
        try:
            totals = swiggy_cart.totals_of(cart_client.fetch())
        except Exception as exc:  # noqa: BLE001
            return PayResult(ok=False, settled=False,
                             detail=f"could not read the Swiggy cart before paying: {str(exc)[:200]}")
        if not totals.found:
            return PayResult(ok=False, settled=False,
                             detail="Swiggy did not report a cart total, so there "
                                    "is no number to check the approval against")

        # 2. Refuse if it is not the number that was approved. checkout() pays
        #    whatever is in the cart, so this is the last gate.
        if abs(totals.total_inr - approved) > _TOLERANCE_INR:
            return PayResult(
                ok=False, settled=False,
                detail=(f"Swiggy's cart is ₹{totals.total_inr:.2f} but ₹{approved:.2f} "
                        f"was approved — refusing to pay a total you did not see"))

        # 3. Ask Swiggy whether it will even let an agent pay, BEFORE trying.
        #    A refusal here costs nothing and produces a sentence the human can
        #    act on; discovering it from a failed checkout produces an error
        #    string and an order in an uncertain state.
        checkout = swiggy_checkout.SwiggyCheckoutClient(token_provider=self._token_provider)
        try:
            checkout.assert_can_pay_from_wallet()
        except swiggy_checkout.WalletNotEligible as exc:
            return PayResult(ok=False, settled=False, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            return PayResult(ok=False, settled=False,
                             detail=f"could not check Swiggy's payment options: {str(exc)[:200]}")

        # 4. Pay. No retries past this line, whatever happens.
        try:
            response = checkout.place_order(address_id=self._address_id,
                                            payment_method=swiggy_checkout.WALLET)
        except swiggy_checkout.CheckoutRefused as exc:
            # Swiggy decided not to place it. Nothing was charged, and that is
            # a clean, reportable outcome — including the likely one here,
            # agenticPaymentEligible being false on this account.
            return PayResult(ok=False, settled=False,
                             detail=f"Swiggy declined to place the order: {exc}")
        except swiggy_checkout.CheckoutUncertain as exc:
            # DO NOT RETRY AND DO NOT GUESS. Re-raised so execute() leaves the
            # pay-intent unsettled and the order in a state that demands
            # reconciliation against Swiggy's order history.
            raise

        merchant_order_id = swiggy_checkout.order_id_of(response)
        if not merchant_order_id:
            # It came back cleanly but without an id. Money has probably moved
            # and we cannot name the order, which is a reconciliation case, not
            # a success.
            raise swiggy_checkout.CheckoutUncertain(
                "Swiggy accepted the checkout but returned no order id")
        return PayResult(ok=True, settled=True, merchant_order_id=merchant_order_id,
                         detail=f"paid ₹{approved:.2f} from Swiggy Money")
