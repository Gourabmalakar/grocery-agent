"""
The worker's entire interface to the outside world. This module is the
enforced half of the process boundary described in the plan: everything the
worker can do is listed here, and nothing here can approve a cart, mint a
PaymentMandate, call a payment rail, or read a credential or delivery
address. If a future change to the worker needs one of those, that is a
sign the architecture is being violated, not a missing convenience function
to add here.

Talks to the Console ONLY over HTTP (grocery.config.MOCKSTORE_URL-style base
URL for the console itself) — no `import console.approval`, ever. That import
boundary is enforced by convention + code review here in Phase 0; a stronger
enforcement (a separate venv/container per process) is a Phase 2+ hardening
item, noted in docs/known-gaps.md.
"""
from __future__ import annotations

from typing import Any

import requests

from grocery import config

CONSOLE_URL = config.CONSOLE_URL


def get_order(order_id: str) -> dict[str, Any]:
    r = requests.get(f"{CONSOLE_URL}/api/order/{order_id}", timeout=10)
    r.raise_for_status()
    return r.json()


def propose_cart(order_id: str, signed_cart: dict[str, Any], comparison: list[dict] | None = None,
                  narrative: str | None = None) -> dict[str, Any]:
    r = requests.post(f"{CONSOLE_URL}/api/order/{order_id}/propose_cart",
                       json={"cart": signed_cart, "comparison": comparison, "narrative": narrative}, timeout=10)
    r.raise_for_status()
    return r.json()


def record_decision(
    *, trace_id: str, actor: str, action: str, reasoning: str,
    evidence: list[dict] | None = None, alternatives_rejected: list[dict] | None = None,
    mandate_ref: str | None = None, confidence: str = "medium", parent_span_id: str | None = None,
) -> str:
    r = requests.post(f"{CONSOLE_URL}/api/decisions", json={
        "trace_id": trace_id, "actor": actor, "action": action, "reasoning": reasoning,
        "evidence": evidence or [], "alternatives_rejected": alternatives_rejected or [],
        "mandate_ref": mandate_ref, "confidence": confidence, "parent_span_id": parent_span_id,
    }, timeout=10)
    r.raise_for_status()
    return r.json()["span_id"]


def killswitch_state() -> dict[str, Any]:
    r = requests.get(f"{CONSOLE_URL}/api/killswitch", timeout=10)
    r.raise_for_status()
    return r.json()
