"""
Phase 3 handoff rail: no credential stored anywhere. Emits a UPI deep-link /
QR payload for the exact approved amount; Gourab pays in his own UPI app.
This is the rail that stores nothing, so it is also the simplest to reason
about — the broker's job here is only to render the link and record that it
was shown, not to know whether the payment actually happened (Phase 3's
open item: reconciling this rail requires either a manual "mark paid" or a
merchant order-status poll).
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from console.rails.base import PaymentRail, PayResult


def build_upi_uri(*, payee_vpa: str, payee_name: str, amount_inr: float, note: str) -> str:
    """Standard UPI deep-link format: upi://pay?pa=...&pn=...&am=...&tn=...
    am is INR with up to 2 decimals; pn and tn must be URL-encoded."""
    return (
        f"upi://pay?pa={quote(payee_vpa)}&pn={quote(payee_name)}"
        f"&am={amount_inr:.2f}&cu=INR&tn={quote(note)}"
    )


class UpiHandoffRail(PaymentRail):
    rail_id = "handoff_upi"

    def __init__(self, payee_vpa_lookup):
        # payee_vpa_lookup: merchant_id -> vpa string, supplied by config —
        # this rail never stores anything of Gourab's, only the merchant's
        # published collection VPA.
        self._lookup = payee_vpa_lookup

    def pay(self, *, payment_mandate: dict[str, Any], cart_ref: str, adapter: Any) -> PayResult:
        # This rail does not itself complete a payment — it hands a link to
        # a human. execute() treats "link shown" as the terminal state for
        # this rail; reconciliation against actual settlement is a Phase-3
        # follow-up, not implemented in Phase 0.
        return PayResult(ok=True, detail="UPI link generated — awaiting human payment in their own app")
