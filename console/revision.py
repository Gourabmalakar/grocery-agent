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

# WHAT THIS IS FOR, NOW THAT THE REVISER EXISTS. These patterns are a cheap
# early catch, not the defence and not the interpreter. The defence is
# structural: max_total_inr lives on the signed intent and NO amendment path
# writes it, so a revision cannot raise the cap however it is phrased. The
# interpreter is worker/reviser, which reads the sentence and answers a budget
# request with a plain "I cannot change the spending limit" — measured, it
# catches "bump it to 2000", "i can pay more", "whatever it costs" and "dont
# worry about the cap", all four of which these patterns miss.
#
# So these have been NARROWED rather than extended. A bare `\bno limit\b` was
# refusing "no limit on brand, any is fine" — a perfectly ordinary revision — and
# a false refusal here is worse than a miss, because a miss still reaches the
# reviser and gets a real answer, while a refusal stops the human dead. Each
# pattern now requires the limit word to actually be about money.
_CAP_RAISE_PATTERNS = [
    r"\bincrease\b.*\b(budget|cap|limit|amount)\b",
    r"\braise\b.*\b(budget|cap|limit|amount)\b",
    r"\bspend more\b",
    r"\bignore\b.*\b(budget|cap|limit)\b",   # "ignore the limit" has no innocent reading
    r"\bno (?:budget|spending|price|cost)\b.*\blimit\b",
    r"\bno limit\b.*\b(budget|spend|price|cost|money)\b",
    r"\bremove\b.*\b(the )?(cap|budget|spending limit)\b",
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
