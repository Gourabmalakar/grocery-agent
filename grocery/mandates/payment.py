"""
PaymentMandate — minted only by the broker (console/approval.py), only after
a human approval event, binding cart_hash + amount + rail + timestamp.

The worker never constructs one of these. There is no function in this
module the worker's restricted tool surface exposes — see worker/tools.py.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from grocery.mandates.signing import sign, save_issued

Rail = Literal["mock", "handoff_upi", "merchant_wallet", "swiggy_mcp"]


def new_payment_mandate(
    *,
    cart: dict[str, Any],
    amount_inr: float,
    rail: Rail,
    approved_by: str,
    approval_factor: str,  # e.g. "passkey", "passkey+passphrase"
) -> dict[str, Any]:
    from grocery.mandates.cart import hash_matches

    if not hash_matches(cart):
        raise ValueError(
            "refusing to mint a PaymentMandate: cart_hash does not match the "
            "cart's current content — this is exactly the drift the hash "
            "exists to catch, and it fails closed."
        )

    payment_id = f"pay_{uuid.uuid4().hex[:12]}"
    payload = {
        "type": "PaymentMandate",
        "payment_id": payment_id,
        "cart_id": cart["cart_id"],
        "cart_hash": cart["cart_hash"],
        "amount_inr": amount_inr,
        "rail": rail,
        "approved_by": approved_by,
        "approval_factor": approval_factor,
        "approved_at": time.time(),
    }
    return sign(payload)


def persist(signed_payment: dict[str, Any]):
    return save_issued("payment", signed_payment["payment_id"], signed_payment)
