"""
CartMandate — what the agent assembled from one merchant. Hashed; the hash
is the thing Gourab actually approves, and the thing execute() re-checks.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from grocery.mandates.signing import sign, save_issued, content_hash


def new_cart(
    *,
    intent_id: str,
    merchant: str,
    line_items: list[dict[str, Any]],
    fees: dict[str, float],
    landed_total_inr: float,
    cart_version: int = 1,
    amendment_ids: list[str] | None = None,
) -> dict[str, Any]:
    """
    line_items: [{sku, name, brand, qty, unit, unit_price_inr, price_source, asof, intent_ref}]
    intent_ref on each item is either the intent_id itself or an amendment_id —
    the lineage pointer cart-auditor checks. An item with no intent_ref, or one
    that doesn't resolve to the real chain, fails the auditor's most important
    check.
    """
    cart_id = f"cart_{uuid.uuid4().hex[:12]}"
    payload = {
        "type": "CartMandate",
        "cart_id": cart_id,
        "intent_id": intent_id,
        "cart_version": cart_version,
        "amendment_ids": amendment_ids or [],
        "merchant": merchant,
        "issued_at": time.time(),
        "line_items": line_items,
        "fees": fees,  # {"delivery": .., "handling": .., "surge": .., "coupon": -..}
        "landed_total_inr": landed_total_inr,
    }
    signed = sign(payload)
    signed["cart_hash"] = content_hash(payload)
    return signed


def persist(signed_cart: dict[str, Any]):
    return save_issued("cart", signed_cart["cart_id"], signed_cart)


def recompute_hash(cart: dict[str, Any]) -> str:
    body = {k: v for k, v in cart.items() if k not in ("signature", "cart_hash")}
    return content_hash(body)


def hash_matches(cart: dict[str, Any]) -> bool:
    """The check execute() runs before touching money: did anything about
    this cart change since it was approved? Any drift, however small, fails
    closed rather than charging a cart the human never saw."""
    return cart.get("cart_hash") == recompute_hash(cart)
