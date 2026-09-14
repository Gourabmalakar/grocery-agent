"""
IntentAmendment — one signed revision instruction from the human, chained to
the intent it amends.

AP2 has no equivalent for this; it's the addition the revision loop needs.
Feedback amends the intent rather than editing the cart directly, because
editing the cart in place would sever the lineage check that
item_without_intent_lineage exists to enforce. See the plan's "revision loop"
section for the full rationale.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from grocery.mandates.signing import sign, save_issued, content_hash

AmendmentKind = Literal[
    "swap_brand", "change_quantity", "drop_item", "add_item", "raise_cap", "other"
]


def new_amendment(
    *,
    intent: dict[str, Any],
    prev_amendment: dict[str, Any] | None,
    instruction_text: str,
    kind: AmendmentKind,
    detail: dict[str, Any],
    requested_by: str = "gourab",
) -> dict[str, Any]:
    """Chain a new amendment onto the intent (or the previous amendment, if
    this is round 2+). prev_hash lets anyone walk the chain back to the
    original signed intent and confirm nothing in between was altered."""
    prev_hash = content_hash(
        {k: v for k, v in (prev_amendment or intent).items() if k != "signature"}
    )
    amendment_id = f"amend_{uuid.uuid4().hex[:12]}"
    payload = {
        "type": "IntentAmendment",
        "amendment_id": amendment_id,
        "intent_id": intent["intent_id"],
        "prev_hash": prev_hash,
        "issued_at": time.time(),
        "requested_by": requested_by,
        "instruction_text": instruction_text,
        "kind": kind,
        "detail": detail,  # e.g. {"drop_sku": "...", "reason": "..."} — shape varies by kind
    }
    return sign(payload)


def is_cap_raise(amendment: dict[str, Any]) -> bool:
    return amendment.get("kind") == "raise_cap"


def persist(signed_amendment: dict[str, Any]):
    return save_issued("amendment", signed_amendment["amendment_id"], signed_amendment)
