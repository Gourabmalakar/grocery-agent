"""
IntentMandate — what Gourab asked for, signed the moment he submits a request.

Every item that ever appears in a cart must trace back to this document (or
to a signed IntentAmendment chained to it — see amendment.py). That lineage
requirement is the single check that makes the rest of the system trustworthy;
see docs/injection-defense.md and the cart-auditor agent.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from grocery.mandates.signing import sign, save_issued

SubstitutionPolicy = Literal["none", "same_brand_only", "any_equivalent"]


def new_intent(
    *,
    items: list[str],
    max_total_inr: int,
    category_allowlist: list[str] | None = None,
    delivery_window: str = "any",
    substitution_policy: SubstitutionPolicy = "same_brand_only",
    requested_by: str = "gourab",
) -> dict[str, Any]:
    """Build and sign a new IntentMandate. Returns the signed document; the
    caller is responsible for persisting it via save_issued (the console does
    this at the point the request is accepted, so an unsaved intent never
    reaches the worker)."""
    intent_id = f"intent_{uuid.uuid4().hex[:12]}"
    payload = {
        "type": "IntentMandate",
        "intent_id": intent_id,
        "issued_at": time.time(),
        "requested_by": requested_by,
        "items": items,
        "category_allowlist": category_allowlist or [],
        "max_total_inr": max_total_inr,
        "delivery_window": delivery_window,
        "substitution_policy": substitution_policy,
        "amendments": [],  # amendment_ids get appended here as they're signed
    }
    return sign(payload)


def persist(signed_intent: dict[str, Any]):
    return save_issued("intent", signed_intent["intent_id"], signed_intent)
