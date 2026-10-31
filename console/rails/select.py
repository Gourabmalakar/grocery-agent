"""Which rail a given payment choice uses. One place, so it cannot disagree.

Two choices are offered and only two:

  "app"     InAppHandoffRail — the basket is already in his Swiggy account and
            he pays there. Nothing in this system can charge anything. Always
            available whenever payments are on at all, and the fallback
            whenever the wallet rail refuses.

  "cod"     SwiggyCodRail — the agent PLACES the order and Gourab pays the
            rider. No instrument touched, nothing drainable, and Swiggy needs to
            enable nothing. The only fully hands-off path available today.

  "wallet"  SwiggyWalletRail — the agent pays from Swiggy Money. Needs
            PAYMENT_BACKEND=wallet, MODE=live, AND Swiggy's own
            agenticPaymentEligible flag on the account.

Nothing else is selectable. MockRail is reachable only from tests, UpiHandoffRail
returns a hard refusal, and the generic WalletRail is a placeholder that fails
closed — none of them can be named here.
"""
from __future__ import annotations

from grocery import config

PAY_IN_APP = "app"
PAY_COD = "cod"
PAY_FROM_WALLET = "wallet"


class RailUnavailable(RuntimeError):
    pass


def available() -> list[str]:
    """Ordered least-authority-first, which is also the order they are shown."""
    if not config.payments_enabled():
        return []
    methods = [PAY_IN_APP]
    if config.cod_enabled():
        methods.append(PAY_COD)
    if config.autonomous_payment_enabled():
        methods.append(PAY_FROM_WALLET)
    return methods


def rail_for(choice: str):
    """Build the rail for a choice, or refuse. Never falls back silently: a
    request for the wallet when the wallet is off must be a refusal, not a
    quiet downgrade to something that behaves differently."""
    choice = (choice or "").strip().lower()
    if choice not in available():
        raise RailUnavailable(
            f"{choice!r} is not an available payment method "
            f"(available: {available() or 'none — payments are off'})")

    if choice == PAY_IN_APP:
        from console.rails.in_app_handoff import InAppHandoffRail
        return InAppHandoffRail()

    from grocery.adapters.swiggy_mcp import _access_token
    address_id = config.SWIGGY_ADDRESS_ID
    if not address_id:
        from grocery.carts.swiggy import SwiggyCartClient
        saved = SwiggyCartClient(token_provider=_access_token).addresses()
        if len(saved) != 1:
            raise RailUnavailable(
                f"swiggy has {len(saved)} saved addresses and nothing says which "
                f"to deliver to — set SWIGGY_ADDRESS_ID before paying")
        address_id = str(saved[0]["id"])

    if choice == PAY_COD:
        from console.rails.swiggy_cod import SwiggyCodRail
        return SwiggyCodRail(token_provider=_access_token, address_id=address_id)

    from console.rails.swiggy_wallet import SwiggyWalletRail
    return SwiggyWalletRail(token_provider=_access_token, address_id=address_id)
