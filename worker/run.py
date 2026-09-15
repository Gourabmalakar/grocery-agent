"""
The shopping worker. A SEPARATE OS PROCESS from the console — run this as
`python -m worker.run <order_id>` in its own terminal/process, never import
console.approval from here. It talks to the console exclusively through
worker/tools.py's HTTP calls.

Phase 0 planning is deterministic (cheapest normalised per-unit price, no
model call) so the whole loop is runnable and testable with zero external
dependencies. The point in the code where an LLM call would plug in for
richer reasoning (interpreting ambiguous free-text revisions, judging
substitutions) is marked _interpret_revision() below — see the cart-reviser
skill, which this function is a deterministic stand-in for.

Structurally, this file has no import of console.approval, grocery.killswitch's
trip/clear functions, any payment rail, or grocery.mandates.payment — that
absence is the point, not an oversight.
"""
from __future__ import annotations

import re
import sys
import time

from grocery import config
from grocery.adapters.mock import MockAdapter, fetch_merchant_meta, all_mock_adapters
from grocery.compare import landed_cost, rank
from grocery.mandates import cart as cart_mandate
from mockstore.catalog import CATALOG
from worker import tools

POLL_SECONDS = 2
MAX_WALL_CLOCK_SECONDS = 15 * 60  # a worker that runs this long is a bug, not patience


def _candidates_for_query(query: str) -> list[str]:
    q = query.lower().strip()
    return [k for k, item in CATALOG.items() if any(t in q or q in t for t in item["query_terms"])]


def _pick_best_listing(item_keys: list[str], merchant: str, *, forced_brand: str | None = None):
    """Among candidate item_keys matching one query, pick the cheapest
    per-normalised-unit in-stock listing this merchant carries. Never reads
    `description` for anything decision-relevant — see injection-defense:
    that field only ever appears in raw_description for optional display,
    and this function does not even access it."""
    from grocery.compare.normalise import normalise

    scored = []
    for key in item_keys:
        listing = CATALOG[key]["listings"].get(merchant)
        if not listing or not listing["in_stock"]:
            continue
        if forced_brand and listing["brand"].lower() != forced_brand.lower():
            continue
        norm = normalise(listing["pack_qty"], listing["pack_unit"], listing["price_inr"])
        scored.append((key, listing, norm.price_per_100_base))

    if not scored:
        return None, None, []

    scored.sort(key=lambda t: t[2])
    best_key, best_listing, _ = scored[0]
    rejected = [
        {"sku": listing["sku"], "why": f"₹{price:.2f}/100-base vs chosen ₹{scored[0][2]:.2f}/100-base"}
        for key, listing, price in scored[1:]
    ]
    return best_key, best_listing, rejected


def _interpret_revision(instruction_text: str) -> dict:
    """Deterministic pattern match for Phase 0. Returns a dict of hints:
    {drop: [terms], brand_swap: {to: brand, away_from: brand} | None}.
    Anything it can't parse is left alone rather than guessed at — a
    revision worker's job is to narrow, never to improvise. A real
    cart-reviser step would replace this with an LLM call constrained to
    the same output shape.

    brand_swap deliberately carries BOTH brands, not just the requested one:
    "Amul not Britannia" must swap only the item category where Britannia is
    actually a candidate (biscuits) — applying "prefer Amul" as a blanket
    filter across every item would silently drop milk/eggs/atta at any
    merchant whose brand for those isn't Amul, which is exactly the
    over-interpretation cart-reviser's skill file warns against. See
    _plan_and_propose's scoping of brand_swap to affected queries only."""
    t = instruction_text.lower()
    hints = {"drop": [], "brand_swap": None}

    m = re.search(r"\b(drop|remove|no)\s+(?:the\s+)?(\w+)", t)
    if m:
        hints["drop"].append(m.group(2))

    m = re.search(r"\b(\w+)\s+not\s+(\w+)", t)  # "Amul not Britannia"
    if m:
        hints["brand_swap"] = {"to": m.group(1), "away_from": m.group(2)}

    return hints


def _plan_and_propose(order_id: str, items: list[str], max_total_inr: int,
                       brand_swap: dict | None = None, drop_terms: list[str] | None = None,
                       cart_version: int = 1, amendment_ids: list[str] | None = None):
    drop_terms = drop_terms or []
    active_items = [
        it for it in items
        if not any(dt in it.lower() for dt in drop_terms)
    ]
    intent_id = order_id_to_intent_id(order_id)
    lineage_ref = (amendment_ids[-1] if amendment_ids else intent_id)

    meta = fetch_merchant_meta()
    per_merchant_lines: dict[str, list[dict]] = {m: [] for m in meta}
    per_merchant_subtotal: dict[str, float] = {m: 0.0 for m in meta}

    for query in active_items:
        item_keys = _candidates_for_query(query)
        if not item_keys:
            tools.record_decision(
                trace_id=order_id, actor="price-scout", action="item_not_found",
                reasoning=f"no catalogue match for '{query}'", confidence="high",
            )
            continue

        # Scope brand_swap to only the queries where the brand being swapped
        # AWAY FROM is actually a candidate somewhere — "Amul not Britannia"
        # must affect biscuits (where Britannia is an option) and nothing
        # else, never every item in the order. Applying "prefer Amul"
        # globally would silently drop milk/eggs/atta wherever their brand
        # isn't Amul, which is the over-interpretation cart-reviser's skill
        # file exists to prevent (see _interpret_revision's docstring).
        query_forced_brand = None
        if brand_swap:
            away = brand_swap["away_from"].lower()
            brands_here = {
                CATALOG[k]["listings"][m]["brand"].lower()
                for k in item_keys for m in CATALOG[k]["listings"]
            }
            if away in brands_here:
                query_forced_brand = brand_swap["to"]

        for merchant in meta:
            best_key, listing, rejected = _pick_best_listing(item_keys, merchant, forced_brand=query_forced_brand)
            if not listing:
                tools.record_decision(
                    trace_id=order_id, actor="price-scout", action="item_unavailable",
                    reasoning=f"'{query}' has no in-stock listing at {merchant}"
                              + (f" matching forced brand '{query_forced_brand}'" if query_forced_brand else ""),
                    confidence="high",
                )
                continue
            line = {
                "sku": listing["sku"], "name": listing["name"], "brand": listing["brand"],
                "qty": 1, "pack_qty": listing["pack_qty"], "pack_unit": listing["pack_unit"],
                "unit_price_inr": listing["price_inr"], "line_total_inr": listing["price_inr"],
                "price_source": "mockstore_page", "asof": time.time(),
                "intent_ref": lineage_ref,  # traces to the intent, or the latest amendment if this is a revision
            }
            per_merchant_lines[merchant].append(line)
            per_merchant_subtotal[merchant] += listing["price_inr"]
            tools.record_decision(
                trace_id=order_id, actor="price-scout", action="select_item",
                reasoning=f"{merchant}: chose {listing['brand']} {listing['name']} for '{query}' "
                          f"at ₹{listing['price_inr']:.2f} — cheapest per normalised unit among candidates",
                evidence=[{"source": merchant, "sku": listing["sku"], "price": listing["price_inr"],
                           "price_source": "mockstore_page", "asof": time.time()}],
                alternatives_rejected=rejected,
                confidence="high" if not rejected else "medium",
            )

    breakdowns = []
    for merchant, subtotal in per_merchant_subtotal.items():
        if not per_merchant_lines[merchant]:
            continue
        breakdowns.append(landed_cost.compute_from_subtotal(merchant, subtotal, meta[merchant]))

    if not breakdowns:
        tools.record_decision(
            trace_id=order_id, actor="shopper", action="no_viable_cart",
            reasoning="no merchant could fulfil any requested item", confidence="high",
        )
        return None

    ranked = rank.rank_by_landed_cost(breakdowns)
    comparison = [
        {
            "merchant": r.breakdown.merchant, "rank": r.rank,
            "items_subtotal_inr": r.breakdown.items_subtotal_inr,
            "delivery_fee_inr": r.breakdown.delivery_fee_inr,
            "landed_total_inr": r.breakdown.landed_total_inr,
            "below_min_order": r.breakdown.below_min_order,
            "cheapest_by_landed_cost": r.cheapest_by_landed_cost,
        }
        for r in ranked
    ]
    tools.record_decision(
        trace_id=order_id, actor="cart-comparator", action="rank_merchants",
        reasoning=f"{ranked[0].breakdown.merchant} wins on landed cost (₹{ranked[0].breakdown.landed_total_inr:.2f})"
                  + (" — NOT the same merchant that wins on item subtotal alone"
                     if not ranked[0].cheapest_by_items_only else ""),
        evidence=comparison, confidence="high",
    )

    winner = ranked[0].breakdown
    if winner.below_min_order:
        tools.record_decision(
            trace_id=order_id, actor="shopper", action="no_viable_cart",
            reasoning=f"every merchant that could fulfil any item is below its own minimum order "
                      f"(cheapest: {winner.merchant}, short by ₹{winner.min_order_shortfall_inr:.2f})",
            confidence="high",
        )
        return None
    if winner.landed_total_inr > max_total_inr:
        tools.record_decision(
            trace_id=order_id, actor="shopper", action="over_budget",
            reasoning=f"cheapest viable cart is ₹{winner.landed_total_inr:.2f}, over the ₹{max_total_inr} cap",
            confidence="high",
        )
        return None

    winner_lines = per_merchant_lines[winner.merchant]
    signed_cart = cart_mandate.new_cart(
        intent_id=intent_id,
        merchant=winner.merchant, line_items=winner_lines,
        fees={"delivery": winner.delivery_fee_inr, "handling": winner.handling_fee_inr, "surge": 0, "coupon": 0},
        landed_total_inr=winner.landed_total_inr, cart_version=cart_version,
        amendment_ids=amendment_ids or [],
    )
    cart_mandate.persist(signed_cart)
    tools.propose_cart(order_id, signed_cart, comparison=comparison)
    return signed_cart


def order_id_to_intent_id(order_id: str) -> str:
    return tools.get_order(order_id)["intent"]["intent_id"]


def main(order_id: str):
    start = time.time()
    order = tools.get_order(order_id)
    items = order["intent"]["items"]
    max_total = order["intent"]["max_total_inr"]

    print(f"[worker] planning for {order_id}: {items} under ₹{max_total}")
    _plan_and_propose(order_id, items, max_total)

    seen_amendments = 0
    while True:
        if time.time() - start > MAX_WALL_CLOCK_SECONDS:
            print("[worker] wall-clock limit reached — stopping")
            return
        ks = tools.killswitch_state()
        if ks["tier"] != "none":
            print(f"[worker] kill switch is {ks['tier']} — stopping immediately")
            return

        order = tools.get_order(order_id)
        state = order["state"]
        if state == "revising":
            amendments = order["amendments"]
            if len(amendments) > seen_amendments:
                latest = amendments[-1]
                hints = _interpret_revision(latest["instruction_text"])
                print(f"[worker] re-planning after revision: {latest['instruction_text']!r} -> {hints}")
                _plan_and_propose(
                    order_id, items, max_total,
                    brand_swap=hints["brand_swap"],
                    drop_terms=hints["drop"],
                    cart_version=order["cart_version"] + 1,
                    amendment_ids=[a["amendment_id"] for a in amendments],
                )
                seen_amendments = len(amendments)
        elif state in ("approved", "executing", "done", "aborted", "refused"):
            print(f"[worker] order reached terminal/human-owned state: {state} — stopping")
            return

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m worker.run <order_id>")
        raise SystemExit(1)
    main(sys.argv[1])
