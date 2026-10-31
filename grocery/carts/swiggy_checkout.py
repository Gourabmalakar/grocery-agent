"""The only client in this repository that can spend money.

EVERYTHING ELSE IN grocery/ IS READ-ONLY OR CART-ONLY. SwiggyCartClient next
door refuses `checkout` by name; this module exists precisely to call it, so it
is kept in its own file with its own allowlist of exactly one tool, and it is
constructed only by console/rails/swiggy_wallet.py, which is itself reachable
only from console/approval.py's execute() after a signed PaymentMandate.

WHAT `checkout` DOES, in Swiggy's own words: "Place and confirm Swiggy Instamart
grocery order. Creates order and confirms payment in a single operation." There
is no authorise-then-capture, no dry run, and no cancel. One call, money gone.

THREE RULES, each from Swiggy's own schema, none of them optional:

  1. `paymentMethod` IS ALWAYS SENT EXPLICITLY. The schema says it "auto-defaults
     to the user's available method if omitted" — so omitting it means the
     merchant picks how to charge, which could be a UPI app, a saved card, or
     cash. A payment method chosen by the payee is not a payment method anybody
     approved.

  2. `confirmDuplicate` IS NEVER SENT. Swiggy's own description: "Never set it
     on your own initiative, and never to retry a failed or timed-out checkout,
     that is how a customer gets charged twice." This module has no parameter
     for it, so no caller can pass it.

  3. NO RETRIES, EVER. A timeout is not a failure — it is an unknown, and the
     only safe response to an unknown payment is to stop and reconcile against
     the merchant's order history. Retrying is how one basket becomes two
     orders.

AGENTIC PAYMENT MAY SIMPLY BE REFUSED. `get_payment_options` on this account
returned `agenticPaymentEligible: false` on 2026-10-13, meaning Swiggy has not
enabled agent-initiated payment here. The rail attempts and reports what comes
back rather than pretending; a refusal from Swiggy is a clean outcome, not an
error to work around.
"""
from __future__ import annotations

from typing import Any

from grocery.mcp.client import McpClient, payload_of

MERCHANT_ID = "swiggy"
MCP_ENDPOINT = "https://mcp.swiggy.com/im"

# Exactly one tool that can spend. `get_payment_options` is a READ — it lists
# methods and eligibility and cannot charge anything — and it is here rather
# than in SwiggyCartClient because that client refuses anything whose name
# contains "payment", which is the right default for a cart builder.
_ALLOWED = frozenset({"checkout", "get_payment_options"})

# Swiggy's payment method GROUPS, verbatim from the checkout schema:
#   "UPI", "Cash"/"COD", "SwiggyPay".
#
# TWO OF THESE ARE AUTONOMOUS AND ONE IS NOT, and the difference is the whole
# reason this constant block has a comment:
#
#   COD        Cash on delivery. The agent places the order; nothing is charged
#              to any instrument, and Gourab pays the rider at the door. This is
#              the ONLY fully hands-off path that works without Swiggy enabling
#              anything — the blast radius is one order he can refuse at the
#              door.
#   SwiggyPay  Swiggy Money, a prepaid balance held BY SWIGGY. Autonomous, but
#              only once Swiggy sets agenticPaymentEligible on the account.
#   UPI        NOT AUTONOMOUS, however much balance is behind it. A UPI intent
#              launches the chosen app (PhonePe, GPay) on the phone for the
#              human to approve. Money in a PhonePe wallet linked to Swiggy does
#              not change that: the approval is the point of UPI. An agent
#              cannot complete it, and this module will not pretend otherwise.
WALLET = "SwiggyPay"

# "Cash", NOT "COD", and the distinction cost a real attempt. The checkout
# schema says: Use one of: "UPI", "Cash"/"COD", "SwiggyPay" — which reads as
# though either spelling works. It does not. The server answered:
#
#     Unsupported payment method "COD". Supported methods: Cash, SwiggyPay.
#
# Note also that get_payment_options reports the COD method with id "COD"
# ({"available": true, "id": "COD", "displayName": "Pay on delivery"}). That id
# is NOT the checkout value — the checkout parameter wants the GROUP name. Two
# different vocabularies for the same thing, in the same API, and the docs use
# the wrong one.
COD = "Cash"

# What this repo is willing to initiate. UPI is deliberately absent.
_AUTONOMOUS = (WALLET, COD)


class CheckoutRefused(RuntimeError):
    """Swiggy declined to place the order. Nothing was charged."""


class CheckoutUncertain(RuntimeError):
    """The call did not come back cleanly, so whether money moved is UNKNOWN.

    Raised instead of returning a failure because the two must never be
    confused: a failure can be retried, an unknown must be reconciled. The
    caller records the intent as unsettled and stops.
    """


class WalletNotEligible(CheckoutRefused):
    """Swiggy has not enabled agent-initiated payment on this account, or the
    wallet is not an available method. Checked BEFORE attempting checkout so
    the answer is a sentence rather than a failed order attempt."""


class SwiggyCheckoutClient:
    def __init__(self, *, token_provider, endpoint: str = MCP_ENDPOINT) -> None:
        self._client = McpClient(endpoint, client_name="grocery-agent",
                                 token_provider=token_provider)
        self._connected = False

    def connect(self) -> dict[str, Any]:
        info = self._client.connect()
        self._connected = True
        return info

    def payment_preflight(self) -> dict[str, Any]:
        """Read-only. What Swiggy says about paying from this account.

        `agenticPaymentEligible` is Swiggy's own gate on agent-initiated
        payment and it is false on this account as of 2026-10-13. There is no
        documented way to flip it and no balance tool on the surface, so the
        honest thing is to report the flag and say what is known rather than
        attempt a checkout and interpret the error.
        """
        if not self._connected:
            self.connect()
        out = payload_of(self._client.call_tool("get_payment_options", {})) or {}
        wallet = out.get("swiggyMoney") or {}
        return {
            "wallet_available": bool(wallet.get("available")),
            "agentic_eligible": bool(out.get("agenticPaymentEligible")),
            "cod_available": bool((out.get("cod") or {}).get("available")),
            "amount_due": out.get("paymentAmount"),
        }

    def assert_can_pay_cod(self) -> dict[str, Any]:
        """COD needs no eligibility flag and no balance — Swiggy either offers
        it for this cart and address or it does not."""
        report = self.payment_preflight()
        if not report["cod_available"]:
            raise CheckoutRefused(
                "Swiggy is not offering cash on delivery for this basket — it "
                "depends on the store, the address and the amount.")
        return report

    def assert_can_pay_from_wallet(self) -> dict[str, Any]:
        report = self.payment_preflight()
        if not report["wallet_available"]:
            raise WalletNotEligible(
                "Swiggy Money is not an available payment method on this "
                "account — add money to the wallet in the Swiggy app first.")
        if not report["agentic_eligible"]:
            raise WalletNotEligible(
                "Swiggy reports agenticPaymentEligible=false for this account, "
                "so it will not let an agent pay on your behalf. Add money to "
                "Swiggy Money in the app — that is what the published flow "
                "describes as enabling it. If it stays false once the wallet is "
                "funded, eligibility is Swiggy-side: builders@swiggy.in.")
        return report

    def place_order(self, *, address_id: str, payment_method: str = WALLET) -> dict[str, Any]:
        """Place and pay for whatever is in the cart. Irreversible.

        Takes no cart argument because Swiggy's cart is bound to the account —
        the caller must have verified the cart contents and total immediately
        before calling this, because there is nothing to verify afterwards.
        """
        if payment_method not in _AUTONOMOUS:
            # Narrower than Swiggy allows, deliberately. A UPI intent needs a
            # human to approve it in the payment app, so an agent "paying" by
            # UPI is an agent handing over a half-finished order.
            raise CheckoutRefused(
                f"{payment_method!r} is not a method this system will initiate. "
                f"Only {WALLET} (prepaid balance) and {COD} (pay the rider) can "
                f"be completed without a human in the payment app."
            )
        if "checkout" not in _ALLOWED:          # pragma: no cover - structural
            raise CheckoutRefused("checkout is not allowlisted")
        if not self._connected:
            self.connect()

        try:
            raw = self._client.call_tool("checkout", {
                "addressId": address_id,
                "paymentMethod": payment_method,
                # confirmDuplicate is deliberately absent. See rule 2 above.
            })
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if _is_a_clean_refusal(message):
                raise CheckoutRefused(message[:300]) from exc
            # Anything else — a timeout, a dropped stream, a 5xx — means we do
            # not know whether Swiggy placed the order.
            raise CheckoutUncertain(message[:300]) from exc

        return payload_of(raw) or {}


# Messages that mean Swiggy decided not to place the order, as opposed to the
# call never completing. Matched conservatively: anything not recognised here is
# treated as UNCERTAIN, because guessing "it failed" about a payment that may
# have succeeded is the expensive direction to be wrong in.
_CLEAN_REFUSALS = (
    "not eligible", "agentic", "not allowed", "unauthorized", "unauthorised",
    "insufficient", "declined", "invalid address", "out of stock",
    "unavailable", "cart is empty", "store is closed", "not serviceable",
    # A REJECTED PARAMETER IS A REFUSAL, NOT AN UNKNOWN. The server validated
    # the request and declined it before doing anything, so no order exists —
    # confirmed against get_orders, which was empty. Treating it as uncertain
    # parked order_0c4c15c9fd71 in NEEDS_RECONCILIATION and told Gourab to go
    # check his Swiggy orders, over a typo in a constant.
    #
    # Erring toward "uncertain" is still right for anything we do NOT recognise
    # — guessing "it failed" about a payment that succeeded is the expensive
    # direction. But a message naming the supported values is not ambiguous.
    "unsupported payment method", "unsupported", "invalid payment",
    "invalid parameter", "missing required", "bad request",
)


def _is_a_clean_refusal(message: str) -> bool:
    low = message.lower()
    return any(marker in low for marker in _CLEAN_REFUSALS)


def order_id_of(response: dict[str, Any]) -> str:
    for key in ("orderId", "order_id", "id", "orderID"):
        value = response.get(key)
        if value:
            return str(value)
    order = response.get("order")
    if isinstance(order, dict):
        return order_id_of(order)
    return ""
