"""
Feedback -> signed IntentAmendment, plus the one guard that must live in code
rather than in a prompt: a revision cannot raise the intent's spending cap.

Classifying free text is the cart-reviser agent's job in the full pipeline
(interpret narrowly, touch only what was asked, show a diff — see the plan).
This module's job is narrower and non-negotiable: no matter how a revision is
classified upstream, a cap increase can only happen through
raise_cap_requires_new_intent(), which forces a brand-new top-level request
rather than a quiet amendment — because a request that started at ₹800 should
not silently become ₹2,000 through a chain of small nudges.
"""
from __future__ import annotations

import re

from console import approval
from grocery.audit import log as audit_log

_CAP_RAISE_PATTERNS = [
    r"\bincrease\b.*\b(budget|cap|limit)\b",
    r"\braise\b.*\b(budget|cap|limit)\b",
    r"\bspend more\b",
    r"\bignore\b.*\b(budget|cap|limit)\b",
    r"\bno limit\b",
    r"\bremove\b.*\b(cap|limit)\b",
    r"\ballow\b.*\bover budget\b",
]


def looks_like_cap_raise(instruction_text: str) -> bool:
    t = instruction_text.lower()
    return any(re.search(p, t) for p in _CAP_RAISE_PATTERNS)


def submit_feedback(order_id: str, instruction_text: str, *, requested_by: str = "gourab") -> approval.Order:
    """Entry point the Console's 'Compare & revise' screen calls. Detects a
    cap-raise attempt BEFORE minting any amendment — automatic_failures'
    `revision_raised_cap` exists precisely so this can never happen quietly."""
    if looks_like_cap_raise(instruction_text):
        audit_log.append("order.revision_cap_raise_blocked", {
            "order_id": order_id, "instruction_text": instruction_text,
        })
        return approval.refuse_revision(
            order_id,
            reason=(
                "This reads as a request to raise the spending cap. A revision "
                "cannot do that — submit a fresh request with the higher amount "
                "if that's really what you want."
            ),
        )
    return approval.request_revision(order_id, instruction_text=instruction_text, requested_by=requested_by)
