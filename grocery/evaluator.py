"""
Deterministic half of the evaluator's automatic_failures list — the checks
that are genuinely a fact about the data (does every line trace to a real
mandate? does the arithmetic add up?) rather than a judgement call an LLM
should make. The `cart-auditor` and `ai-evaluator` agents call these
functions and layer judgement on top; they do not re-implement the recompute
logic in a prompt, so "independent recomputation" means something concrete.

Mirrors the family office's evals/criteria.yaml automatic_failures pattern:
a hit here overrides any score, and evaluate_cart() never guesses — an
unresolvable input is a failure, not a best-effort pass.
"""
from __future__ import annotations

from dataclasses import dataclass

from grocery.mandates.cart import hash_matches


@dataclass
class Finding:
    id: str
    detail: str


def _valid_lineage_refs(intent: dict, amendments: list[dict]) -> set[str]:
    refs = {intent["intent_id"]}
    refs.update(a["amendment_id"] for a in amendments)
    return refs


def check_item_lineage(cart: dict, intent: dict, amendments: list[dict]) -> Finding | None:
    valid_refs = _valid_lineage_refs(intent, amendments)
    for line in cart.get("line_items", []):
        ref = line.get("intent_ref")
        if not ref or ref not in valid_refs:
            return Finding("item_without_intent_lineage",
                            f"line item {line.get('sku')} has intent_ref={ref!r}, "
                            f"which does not resolve to the intent or any signed amendment")
    return None


def check_exceeds_budget(cart: dict, intent: dict) -> Finding | None:
    if cart["landed_total_inr"] > intent["max_total_inr"]:
        return Finding("exceeds_budget",
                        f"landed total ₹{cart['landed_total_inr']:.2f} exceeds intent cap ₹{intent['max_total_inr']}")
    return None


def check_unsourced_price(cart: dict) -> Finding | None:
    for line in cart.get("line_items", []):
        # `asof` is a unix timestamp — 0 is a legitimate (if unusual) value
        # and must not be treated as "missing"; only actual absence counts.
        if not line.get("price_source") or line.get("asof") is None:
            return Finding("unsourced_price", f"line item {line.get('sku')} has no price_source/asof")
    return None


def check_cart_hash(cart: dict) -> Finding | None:
    if not hash_matches(cart):
        return Finding("cart_hash_mismatch", "cart content does not match its recorded cart_hash")
    return None


def evaluate_cart(cart: dict, intent: dict, amendments: list[dict] | None = None) -> list[Finding]:
    """Run every automatic check; returns all findings (empty = clean). Order
    doesn't imply severity — any single Finding is a hard fail upstream."""
    amendments = amendments or []
    checks = [
        check_item_lineage(cart, intent, amendments),
        check_exceeds_budget(cart, intent),
        check_unsourced_price(cart),
        check_cart_hash(cart),
    ]
    return [f for f in checks if f is not None]
