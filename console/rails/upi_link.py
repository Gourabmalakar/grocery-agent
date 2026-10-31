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
        """NOT SELECTABLE, and it returns a failure rather than a success.

        This rail never completed a payment — it rendered a deep link and
        returned ok=True, which execute() read as "paid". The order went to
        DONE and record_spend() wrote a spend that had not happened, so the
        caps were consumed by money nobody had sent and the merchant was still
        waiting. The rail could not even tell whether the human paid.

        PayResult now distinguishes settled from handed-off, so a hand-off has
        an honest representation — but it belongs to InAppHandoffRail, which
        hands over a real basket in the merchant's own app rather than a bare
        UPI string with no order behind it. This stays as a hard refusal so
        nothing can select it by accident.
        """
        return PayResult(
            ok=False,
            detail="the bare UPI link rail is not selectable — it cannot tell "
                   "whether a payment happened. Use the in-app hand-off.",
            settled=False,
        )
