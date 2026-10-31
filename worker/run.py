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

import json
import math
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path

from grocery import config
from grocery.adapters.base import Listing
from grocery.compare import landed_cost, rank

from grocery.compare import normalise as normalise_mod
from worker import auditor, reviser
from grocery.mandates import cart as cart_mandate
from grocery import narrator
from worker import backends, tools

POLL_SECONDS = 2
MAX_WALL_CLOCK_SECONDS = 15 * 60  # a worker that runs this long is a bug, not patience


def _pick_best_from_listings(listings: list[Listing], *, forced_brand: str | None = None,
                             query: str = "", use_model: bool = False,
                             banned_names: list[str] | None = None,
                             required_words: list[str] | None = None):
    """The cheapest in-stock listing per normalised unit THAT ACTUALLY ANSWERS
    THE QUESTION. Returns (best, rejected); best is a Listing or None.

    The second clause is new and is the fix for order_6a45f91aaa2e, where this
    function returned Tata Salt for '30 eggs', an onion for '500g oranges' and a
    potato for '250 gm mausambi'. It was not malfunctioning: JioMart's catalogue
    answered those phrases with those products, and among them those were
    genuinely the cheapest per unit. Price was being asked to choose before
    anything had established that the candidates were the same kind of thing.

    So candidates the auditor calls a MISMATCH are removed BEFORE price sorts
    anything, and each one is recorded in `rejected` with the auditor's sentence
    — which is what lets the trace say "skipped Tata Salt: nothing in it matches
    'eggs'" instead of quietly buying it. If every candidate is a mismatch the
    answer is None, and the caller reports the item unavailable at that merchant,
    which is true and useful, where a confident wrong pick was neither.

    `query` defaults to empty, under which no auditing happens at all. That keeps
    the mock path and every existing caller behaving exactly as before.
    """
    from grocery.compare.normalise import normalise
    from worker import auditor, reviser

    scored = []
    rejected: list[dict] = []
    banned = {n.strip().lower() for n in (banned_names or []) if n.strip()}
    for listing in listings:
        if not listing.in_stock:
            continue
        if banned and listing.name.strip().lower() in banned:
            # The human looked at this exact product and said no. Offering it
            # again next round is how a revision loop stops feeling like one.
            rejected.append({"sku": listing.sku, "name": listing.name,
                             "why": "you asked me not to buy this one"})
            continue
        if forced_brand and listing.brand.lower() != forced_brand.lower():
            continue
        if query:
            verdict = auditor.satisfies(query, name=listing.name, brand=listing.brand,
                                        category=listing.category, required=required_words)
            if verdict.is_rejection:
                rejected.append({"sku": listing.sku, "name": listing.name,
                                 "why": f"does not answer {query!r}: {verdict.why}"})
                continue
        norm = normalise(listing.pack_qty, listing.pack_unit, listing.price_inr)
        scored.append((listing, norm.price_per_100_base))

    if not scored:
        return None, rejected

    scored.sort(key=lambda t: t[1])
    best, _ = scored[0]

    # ASK THE MODEL ONLY WHEN THE ANSWER IS ACTUALLY IN DOUBT.
    #
    # Gated on two things, both of which matter. With a single surviving
    # candidate there is nothing to choose between, so a call would buy
    # nothing but latency and quota. And when the cheapest IS the store's own
    # top result, the two signals already agree — it is where they DISAGREE
    # that the deterministic rule went wrong: Swiggy ranked "Pillsbury
    # Multigrain Atta ₹305" first and cheapest-per-100g bought "Pillsbury
    # Chakki Fresh Atta ₹470", the wrong product at a higher price.
    #
    # The model returns an index into `survivors`; price and sku are then read
    # from our own Listing, so it can reorder a list and nothing else.
    # ASKED WHENEVER THERE IS A CHOICE TO MAKE. This used to be gated on the
    # cheapest pick differing from the store's own top result — a call-count
    # saving that quietly decided the hardest cases on word overlap alone. In
    # the run that prompted this, "Amul Fresh Butter Cookies" was BOTH the
    # cheapest per unit AND the store's first result for "amul butter", so the
    # two signals agreed, the model was never consulted, and biscuits went into
    # the basket. The cache makes a repeated question free; a wrong product is
    # not free.
    # ONE SURVIVING CANDIDATE STILL NEEDS JUDGING. Requiring two was the last
    # remnant of the call-count gate, and it failed the same way: the auditor
    # rejected the buttermilk on word overlap, leaving "Amul Fresh Butter
    # Cookies" alone in the list — and a list of one skipped the model entirely
    # and bought biscuits. "Is this the thing?" is a question about the product,
    # not about how many products there are.
    survivors = [listing for listing, _ in scored]
    if use_model and query and survivors:
        from worker.llm import shopping as llm_shopping
        verdict = llm_shopping.choose(query, survivors)
        if verdict is not None:
            if verdict["index"] is None:
                # HONOURED — the model may refuse the whole set. Tested against
                # the real catalogue it gets this right in both directions: it
                # picks "Amul Pasteurised Butter 100 g" for a 250g request
                # (size is not identity) and refuses a list of nothing but
                # cookies and buttermilk.
                #
                # What made the earlier refusal a bug was not the refusal — it
                # was that the item then vanished with no reason a human could
                # read. The reason now travels with it.
                # "NOTHING MATCHES" IS ADVISORY, NOT FINAL — and this is the
                # asymmetry that decides it. A wrong pick is flagged and the
                # human sees it; a dropped item just is not there, and nothing
                # on the screen says why. So the model may not empty a line the
                # deterministic auditor was willing to accept: it may only doubt
                # it, loudly, in the trace.
                #
                # Gourab asked for butter in three separate revisions and it
                # never appeared. The store had "Amul Pasteurised Butter 100 g";
                # the auditor passed it; the model answered "none of these match
                # the requested 250g size" and the item vanished every time.
                # Pack size is not identity — three 100g packs cover 250g, which
                # the quantity logic does on its own one step later.
                rejected.append({"sku": "", "name": "",
                                 "why": f"none of these answer {query!r}: {verdict['why']}"})
                print(f"[worker] nothing at this store answers {query!r}: {verdict['why']}")
                _MODEL_REFUSALS[query] = verdict["why"]
                return None, rejected
            chosen = survivors[verdict["index"]]
            if chosen is not best:
                rejected.append({"sku": best.sku, "name": best.name,
                                 "why": f"cheapest per unit, but the model preferred "
                                        f"{chosen.name!r}: {verdict['why']}"})
            best = chosen
    rejected += [
        {"sku": listing.sku, "name": listing.name,
         "why": f"₹{price:.2f}/100-base vs chosen ₹{scored[0][1]:.2f}/100-base"}
        for listing, price in scored[1:]
    ]
    return best, rejected


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

    m = re.search(r"\b(drop|remove)\s+(?:the\s+)?(\w+)", t)
    if m:
        hints["drop"].append(m.group(2))

    # "Amul not Britannia" — both sides must be alphabetic (a brand name),
    # never digits. Without that restriction this matched "30 eggs not 6"
    # as a brand swap from "6" to "eggs", a real false positive caught by
    # hand: it silently did nothing (no real brand is ever "6"), while the
    # actual request — 30 eggs instead of 6 — went completely unaddressed.
    m = re.search(r"\b([a-z]+)\s+not\s+([a-z]+)\b", t)
    if m:
        hints["brand_swap"] = {"to": m.group(1), "away_from": m.group(2)}

    return hints


def note_if_revision_not_understood(order_id: str, instruction_text: str, hints: dict) -> bool:
    """Returns True if `hints` carries an actionable change. If not, records
    a `revision_not_understood` DecisionRecord explaining exactly why —
    rather than letting the caller silently re-propose an unchanged cart
    with no trace of what went wrong. Factored out of main()'s polling loop
    so it is exercised directly by tests, not only by the loop that happens
    to call it in production."""
    actionable = bool(hints["drop"]) or hints["brand_swap"] is not None
    if not actionable:
        tools.record_decision(
            trace_id=order_id, actor="shopper", action="revision_not_understood",
            reasoning=(
                f"Could not extract an actionable change from: {instruction_text!r}. "
                f"Phase 0's revision interpreter is pattern-based and only understands "
                f"'drop/remove <item>' and '<brand> not <brand>' where both are catalogue "
                f"brand names — it does not parse quantity corrections, requests for items or "
                f"brands outside the catalogue, or general complaints. The cart below is "
                f"otherwise unchanged from before this feedback."
            ),
            confidence="high",
        )
    return actionable


def _plan_and_propose(order_id: str, items: list[str], max_total_inr: int,
                       brand_swap: dict | None = None, drop_terms: list[str] | None = None,
                       forced_brands: dict[str, str] | None = None,
                       rejected_products: dict[str, list[str]] | None = None,
                       reviser_questions: list[str] | None = None,
                       cart_version: int = 1, amendment_ids: list[str] | None = None,
                       revision_instruction: str | None = None, revision_not_understood: bool = False,
                       revision_could_not_be_applied: bool = False,
                       reuse_listings: bool = False,
                       adapters: list | None = None):
    drop_terms = drop_terms or []
    active_items = [
        it for it in items
        if not any(dt in it.lower() for dt in drop_terms)
    ]
    intent_id = order_id_to_intent_id(order_id)
    lineage_ref = (amendment_ids[-1] if amendment_ids else intent_id)
    reset_listing_cache(order_id)
    _MODEL_REFUSALS.clear()

    if adapters is None:
        adapters = backends.adapters_for_backend()
    meta = backends.fee_metadata()

    # READ THE SHOPPING LIST BEFORE SHOPPING IT. One call for the whole list,
    # turning each line into the phrase a merchant's search box actually wants
    # and the quantity that was asked for. Measured: JioMart searched for
    # "250 gm mausambi" returns onions and potatoes; searched for "mosambi" it
    # returns "Mosambi 6 pcs (Pack)" first. Same catalogue, same day.
    #
    # None means the model was unavailable, and the whole order then runs on
    # the raw text exactly as before — never a mix of the two within one cart.
    from worker.llm import client as llm_client, shopping as llm_shopping
    understanding = llm_shopping.understand_all(list(active_items)) or {}
    model_usable, model_why = llm_client.available()
    tools.record_decision(
        trace_id=order_id, actor="shopper",
        action="list_understood" if understanding else "list_not_understood",
        reasoning=(
            "; ".join(f"{line!r} -> search {v['search_phrase']!r}"
                      + (f", quantity {v['quantity_value']:g}{v['quantity_unit']}"
                         if v["quantity_value"] and v["quantity_unit"] else "")
                      for line, v in understanding.items())[:900]
            if understanding else
            f"read the list literally — {model_why or 'the model returned nothing usable'}"
        ),
        confidence="high" if understanding else "low",
    )

    merchants = [a.merchant_id for a in adapters]
    per_merchant_lines: dict[str, list[dict]] = {m: [] for m in merchants}
    per_merchant_subtotal: dict[str, float] = {m: 0.0 for m in merchants}
    not_found_queries: list[str] = []
    query_scope: dict[str, dict] = {}
    blocked_merchants: set[str] = set()

    for query in active_items:
        # Search every adapter for this query
        listings_by_merchant: dict[str, list[Listing]] = {}
        any_found = False
        search_phrase = understanding.get(query, {}).get("search_phrase") or query
        # Normalised, because the key has to survive the model phrasing the same
        # request slightly differently between rounds. "Pillsbury multigrain
        # atta" and "pillsbury multigrain atta" are the same search, and a cache
        # that misses on a capital letter is a cache that silently does nothing.
        cache_key = " ".join(search_phrase.lower().split())
        cached = _LISTING_CACHE.get(cache_key) if reuse_listings else None
        if cached is not None:
            # Same phrase as a previous round: the catalogue for it is already
            # in hand. Selection still re-runs below — only the network does not.
            listings_by_merchant = {m: list(ls) for m, ls in cached.items()}
            any_found = any(listings_by_merchant.values())
            print(f"[worker] reusing {sum(len(v) for v in listings_by_merchant.values())} "
                  f"listings for {search_phrase!r} — unchanged by this revision")
        else:
            for adapter in adapters:
                if adapter.merchant_id in blocked_merchants:
                    listings_by_merchant[adapter.merchant_id] = []
                    continue
                try:
                    results = adapter.search(search_phrase)
                    listings_by_merchant[adapter.merchant_id] = results
                    if results:
                        any_found = True
                except Exception as e:
                    # A MERCHANT THAT BLOCKS US BLOCKS US FOR THE WHOLE RUN.
                    # Blinkit answers a bot-detection page, and asking it again
                    # for the next item launches another browser, waits for
                    # another page, and gets the same refusal — about ten
                    # seconds each, repeated per item, to learn something
                    # already known. It is also the least polite thing to do to
                    # a site that has just said no.
                    if type(e).__name__ in ("AdapterBlocked", "AdapterUnavailable"):
                        blocked_merchants.add(adapter.merchant_id)
                        print(f"[worker] {adapter.merchant_id} is refusing us "
                              f"({e}) — leaving it out of the rest of this run")
                    else:
                        print(f"[worker] {adapter.merchant_id} search failed for '{query}': {e}")
                    listings_by_merchant[adapter.merchant_id] = []
            _LISTING_CACHE[cache_key] = {m: list(ls) for m, ls in listings_by_merchant.items()}
            _save_listing_cache(order_id)

        if not any_found:
            not_found_queries.append(query)
            tools.record_decision(
                trace_id=order_id, actor="price-scout", action="item_not_found",
                reasoning=f"no catalogue match for '{query}'", confidence="high",
            )
            continue

        # Scope brand_swap to only the queries where the brand being swapped
        # AWAY FROM is actually a candidate somewhere — "Amul not Britannia"
        # must affect biscuits (where Britannia is an option) and nothing
        # else, never every item in the order.
        # The reviser names the item it means, so no scoping heuristic is
        # needed — that guesswork existed only because a regex could not say
        # which item "Amul not Britannia" was about.
        query_forced_brand = (forced_brands or {}).get(query)
        if not query_forced_brand and brand_swap:
            away = brand_swap["away_from"].lower()
            all_brands = {
                listing.brand.lower()
                for merchant_listings in listings_by_merchant.values()
                for listing in merchant_listings
            }
            if away in all_brands:
                query_forced_brand = brand_swap["to"]

        query_scope[query] = {"forced_brand": query_forced_brand, "listings_by_merchant": listings_by_merchant}

        # The model's reading wins where it has one: parse_quantity_phrase only
        # sees a LEADING number, so "atta 5kg" and "a dozen eggs" were both
        # invisible to it and silently bought one pack of whatever.
        requested = _requested_quantity(understanding.get(query), query)

        for merchant in merchants:
            merchant_listings = listings_by_merchant.get(merchant, [])
            best, rejected = _pick_best_from_listings(
                merchant_listings, forced_brand=query_forced_brand, query=query,
                use_model=model_usable,
                banned_names=(rejected_products or {}).get(query),
                required_words=understanding.get(query, {}).get("must_have"))
            if not best:
                tools.record_decision(
                    trace_id=order_id, actor="price-scout", action="item_unavailable",
                    reasoning=f"'{query}' has no in-stock listing at {merchant}"
                              + (f" matching forced brand '{query_forced_brand}'" if query_forced_brand else ""),
                    evidence=[{"source": merchant}],
                    confidence="high",
                )
                continue

            norm = normalise_mod.normalise(best.pack_qty, best.pack_unit, best.price_inr)
            qty = 1
            qty_note = ""
            if requested is not None:
                req_amount, req_kind = requested
                if req_kind == norm.base_kind:
                    qty = max(1, math.ceil(req_amount / norm.base_quantity))
                    qty_note = (f" — requested {req_amount:g} {req_kind}, "
                                f"{qty} x {best.pack_qty}{best.pack_unit} pack(s) covers it")
                else:
                    qty_note = (f" — requested quantity ('{query}') is a {req_kind} but this item is sold by "
                                f"{norm.base_kind}; defaulting to 1 pack rather than guess a conversion")

            line_total = best.price_inr * qty
            line = {
                "sku": best.sku, "name": best.name, "brand": best.brand,
                "qty": qty, "pack_qty": best.pack_qty, "pack_unit": best.pack_unit,
                "unit_price_inr": best.price_inr, "line_total_inr": line_total,
                "price_source": best.price_source, "asof": best.asof,
                "intent_ref": lineage_ref,
                # The request phrase this line is answering. Distinct from
                # intent_ref, which points at the intent/amendment for lineage:
                # this says WHICH of the five things asked for it is supposed to
                # be, which is what the auditor judges it against.
                "intent_query": query,
            }
            per_merchant_lines[merchant].append(line)
            per_merchant_subtotal[merchant] += line_total
            tools.record_decision(
                trace_id=order_id, actor="price-scout", action="select_item",
                reasoning=f"{merchant}: chose {best.brand} {best.name} x{qty} for '{query}' "
                          f"at ₹{best.price_inr:.2f} each (₹{line_total:.2f} total) — cheapest per "
                          f"normalised unit among candidates{qty_note}",
                evidence=[{"source": merchant, "sku": best.sku, "price": best.price_inr,
                           "price_source": best.price_source, "asof": best.asof}],
                alternatives_rejected=rejected,
                confidence="high" if not rejected else "medium",
            )

    breakdowns = []
    for merchant, subtotal in per_merchant_subtotal.items():
        if not per_merchant_lines[merchant]:
            continue
        if merchant in meta:
            breakdowns.append(landed_cost.compute_from_subtotal(merchant, subtotal, meta[merchant]))
        else:
            breakdowns.append(landed_cost.compute_subtotal_only(merchant, subtotal))

    if not breakdowns:
        tools.record_decision(
            trace_id=order_id, actor="shopper", action="no_viable_cart",
            reasoning="no merchant could fulfil any requested item", confidence="high",
        )
        return None

    # FULFILMENT BEFORE PRICE. Ranking on cost alone compares carts that hold
    # different things, and in order_6a45f91aaa2e that is exactly what went
    # wrong twice over: JioMart "won" at ₹631 because salt-instead-of-eggs is
    # cheaper than eggs, and on the revision before it Blinkit "won" at ₹158
    # having found only two of the five items at all. A basket that answers
    # three of five questions is not a cheaper answer to all five, so the
    # comparison is between merchants that fulfil the MOST of the ask, and only
    # then on price. `fulfilment` carries the counts so the reason is visible.
    asked = len(active_items)
    fulfilled = {m: len({line["intent_query"] for line in per_merchant_lines[m]})
                 for m in merchants}
    ranked = rank.rank_by_landed_cost(breakdowns, fulfilment=fulfilled)
    comparison = [
        {
            "merchant": r.breakdown.merchant, "rank": r.rank,
            "items_subtotal_inr": r.breakdown.items_subtotal_inr,
            "delivery_fee_inr": r.breakdown.delivery_fee_inr,
            "landed_total_inr": r.breakdown.landed_total_inr,
            "below_min_order": r.breakdown.below_min_order,
            "cheapest_by_landed_cost": r.cheapest_by_landed_cost,
            "items_fulfilled": fulfilled.get(r.breakdown.merchant, 0),
            "items_requested": asked,
            # THE OTHER CARTS, IN FULL. Gourab asked to see the three carts and
            # not only the winner, and he is right that a recommendation whose
            # alternatives are invisible cannot be checked. These are the same
            # line dicts the winning cart is built from.
            "line_items": [
                {k: v for k, v in line.items() if k != "intent_query"}
                for line in per_merchant_lines[r.breakdown.merchant]
            ],
            "unfulfilled_items": [
                item for item in active_items
                if item not in {l["intent_query"] for l in per_merchant_lines[r.breakdown.merchant]}
            ],
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

    # intent_query is worker bookkeeping; the signed cart keeps the schema it
    # already had, so cart_hash and every tests/state expectation are untouched.
    winner_lines = [{k: v for k, v in line.items() if k != "intent_query"}
                    for line in per_merchant_lines[winner.merchant]]
    winner_queries = {str(line["sku"]): line["intent_query"]
                      for line in per_merchant_lines[winner.merchant]}
    signed_cart = cart_mandate.new_cart(
        intent_id=intent_id,
        merchant=winner.merchant, line_items=winner_lines,
        fees={"delivery": winner.delivery_fee_inr, "handling": winner.handling_fee_inr, "surge": 0, "coupon": 0},
        landed_total_inr=winner.landed_total_inr, cart_version=cart_version,
        amendment_ids=amendment_ids or [],
    )
    cart_mandate.persist(signed_cart)

    missing_at_winner = [
        query for query, scope in query_scope.items()
        if _pick_best_from_listings(
            scope["listings_by_merchant"].get(winner.merchant, []),
            forced_brand=scope["forced_brand"], query=query,
        )[0] is None
    ]

    # THE WHOLE-CART AUDIT, run before the human is shown anything. Each line is
    # re-judged against the phrase it was chosen for — independently of the
    # picker, which has already made its case — and the gaps are recorded and
    # then said out loud. This is deliberately a SECOND look rather than a
    # re-use of the picker's own verdict: the picker's job is to choose well,
    # and something whose only check is its own opinion of itself is unchecked.
    # What every OTHER merchant charged for each request, so the auditor can
    # notice a line that is priced like a different category of object.
    peer_prices: dict[str, list[float]] = {}
    for merchant, lines_for_merchant in per_merchant_lines.items():
        if merchant == winner.merchant:
            continue
        for line in lines_for_merchant:
            peer_prices.setdefault(line["intent_query"], []).append(float(line["line_total_inr"]))

    audit = auditor.audit_cart(requested_items=list(active_items),
                               line_items=winner_lines, line_queries=winner_queries,
                               peer_prices=peer_prices)
    # Why an item is missing, in the model's own words, above the approve button.
    for missing_item in list(audit.missing):
        reason = _MODEL_REFUSALS.get(missing_item)
        if reason:
            audit.suspects.append(auditor.LineVerdict(
                missing_item, "", auditor.SUSPECT,
                f"nothing any store offered looked like it — {reason}"))
    if not audit.ok or audit.suspects:
        tools.record_decision(
            trace_id=order_id, actor="cart-auditor",
            action="cart_audit_failed" if not audit.ok else "cart_audit_flagged",
            reasoning="; ".join(audit.feedback())[:900],
            evidence=[{"mismatches": [v.name for v in audit.mismatches],
                       "missing": audit.missing,
                       "suspect": [v.name for v in audit.suspects]}],
            confidence="high",
        )
    else:
        tools.record_decision(
            trace_id=order_id, actor="cart-auditor", action="cart_audit_passed",
            reasoning=f"every one of the {len(active_items)} requested items is answered by a "
                      f"line that matches it",
            confidence="high",
        )

    if revision_could_not_be_applied:
        narrative = narrator.narrate_revision_could_not_be_applied(instruction_text=revision_instruction or "")
    elif revision_not_understood:
        narrative = narrator.narrate_revision_not_understood(instruction_text=revision_instruction or "")
    else:
        narrative = narrator.narrate_proposal(
            cart=signed_cart, comparison=comparison,
            missing_items=missing_at_winner, not_found_items=not_found_queries,
            max_total_inr=max_total_inr, revision_instruction=revision_instruction,
            fees_known=winner.fees_known,
            audit_notes=audit.feedback(),
            reviser_questions=reviser_questions,
        )

    tools.propose_cart(order_id, signed_cart, comparison=comparison, narrative=narrative)
    _LAST_LINE_QUERIES.clear()
    _LAST_LINE_QUERIES.update(winner_queries)
    return signed_cart


def _last_cart_for(intent_id: str) -> dict | None:
    """The most recent cart issued against this intent.

    request_revision sets order.cart = None — correctly, since a revision
    invalidates the prior cart entirely. But the reviser has to see what the
    human was looking at when they typed: "remove onion" is about a line on a
    screen, not about the shopping list. Mandates are append-only on disk
    (save_issued refuses to overwrite), so the cart is still there to read even
    when the worker has just started and has no memory of proposing it.
    """
    try:
        carts = []
        for path in config.MANDATES_ISSUED.glob("cart-*.json"):
            try:
                cart = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if cart.get("intent_id") == intent_id:
                carts.append(cart)
        if not carts:
            return None
        return max(carts, key=lambda c: (c.get("cart_version", 0), c.get("issued_at", 0)))
    except (OSError, json.JSONDecodeError) as exc:
        # NARROW ON PURPOSE. This was `except Exception`, and it hid a missing
        # `import json` for the whole life of the function: every call raised
        # NameError, was swallowed, and returned None. The reviser therefore
        # never saw the previous cart, so a revision naming a PRODUCT rather
        # than a list entry could never resolve — the exact feature this
        # function exists to serve, broken silently by its own safety net.
        print(f"[worker] could not read the previous cart ({exc})")
        return None


def _cart_lines_for_reviser(cart: dict | None, line_queries: dict[str, str]) -> list[dict]:
    """Product names plus the list entry each one answers. No sku, no price
    source, no identifiers — the reviser needs to recognise what the human saw
    and nothing more (worker/llm/privacy.py)."""
    if not cart:
        return []
    return [{"name": str(line.get("name", "")),
             "for_item": line_queries.get(str(line.get("sku", "")), "")}
            for line in cart.get("line_items", [])]


def order_id_to_intent_id(order_id: str) -> str:
    return tools.get_order(order_id)["intent"]["intent_id"]


def main(order_id: str):
    """Safe to run (or re-run) against an order in ANY state — including one
    a previous worker process already proposed a cart for, or one sitting on
    a pending revision nobody was running to pick up. Caught by hand: running
    this a second time against an already-proposed order crashed outright
    with an unhandled 409, because the old version always called
    _plan_and_propose() unconditionally as if the order were always fresh."""
    start = time.time()
    order = tools.get_order(order_id)
    items = order["intent"]["items"]
    max_total = order["intent"]["max_total_inr"]
    state = order["state"]
    amendments = order.get("amendments", [])

    adapters = backends.adapters_for_backend()
    print(f"[worker] backend={config.MERCHANT_BACKEND}, adapters: {[a.merchant_id for a in adapters]}")

    # The list as it stands after any revisions so far. `items` stays the
    # original ask, so a revision that cannot be applied can fall back to it.
    current_items = list(items)

    if state == "draft":
        print(f"[worker] planning for {order_id}: {items} under ₹{max_total}")
        _plan_and_propose(order_id, items, max_total, adapters=adapters)
        seen_amendments = 0
    elif state == "revising":
        print(f"[worker] order {order_id} has a pending revision waiting — picking it up now")
        seen_amendments = len(amendments) - 1  # let the loop below treat the latest amendment as new
    else:
        print(f"[worker] order {order_id} is already '{state}' — nothing to plan; watching for a "
              f"further revision or a terminal state")
        seen_amendments = len(amendments)

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
                instruction = latest["instruction_text"]
                cart_version = order["cart_version"] + 1
                amendment_ids = [a["amendment_id"] for a in amendments]

                # THE REVISER READS THE SENTENCE AND THE CART TOGETHER. The two
                # regexes this replaces could not do either: one of them matched
                # the "no i" in "no i need 2 kg atta", produced drop=["i"],
                # substring-matched that against "pilsbury" and "multigrain",
                # and emptied the cart (order_59eaa8e03b2b). The other could not
                # see the cart at all, so "remove onion" — about a product
                # chosen for "500g orhanges" — matched nothing and did nothing.
                previous_cart = _last_cart_for(order_id_to_intent_id(order_id))
                plan = reviser.revise(
                    instruction, items=current_items,
                    cart_lines=_cart_lines_for_reviser(previous_cart, _LAST_LINE_QUERIES),
                )
                applied = reviser.apply(
                    plan, items=current_items,
                    line_to_item={l["name"]: l["for_item"]
                                  for l in _cart_lines_for_reviser(previous_cart, _LAST_LINE_QUERIES)},
                )
                _record_revision(order_id, instruction, plan, applied, current_items)

                if plan.unavailable:
                    # NO SILENT FALLBACK TO THE REGEX. A wrong reading that looks
                    # like a right one is what emptied the cart; an admitted one
                    # is survivable.
                    print(f"[worker] revision interpreter unavailable: {plan.unavailable_reason}")
                    _plan_and_propose(
                        order_id, current_items, max_total,
                        cart_version=cart_version, amendment_ids=amendment_ids,
                        revision_instruction=instruction,
                        revision_not_understood=True,
                        reuse_listings=True, adapters=adapters,
                    )
                    seen_amendments = len(amendments)
                    continue

                if not plan.actionable:
                    # Questions and refusals are answers, not failures: the human
                    # gets the cart back with the question attached.
                    print(f"[worker] revision needs a reply: "
                          f"{plan.questions() + plan.refusals()}")
                    _plan_and_propose(
                        order_id, current_items, max_total,
                        cart_version=cart_version, amendment_ids=amendment_ids,
                        revision_instruction=instruction,
                        reviser_questions=plan.questions() + plan.refusals(),
                        reuse_listings=True, adapters=adapters,
                    )
                    seen_amendments = len(amendments)
                    continue

                current_items = applied["items"]
                print(f"[worker] re-planning after revision: {instruction!r} -> "
                      f"{[f'{o.op}:{o.target or o.value}' for o in plan.ops]}")
                result = _plan_and_propose(
                    order_id, current_items, max_total,
                    forced_brands=applied["forced_brands"],
                    rejected_products=applied["rejected_products"],
                    reuse_listings=True,
                    cart_version=cart_version,
                    amendment_ids=amendment_ids,
                    revision_instruction=instruction,
                    reviser_questions=plan.questions() + plan.refusals(),
                    adapters=adapters,
                )
                if result is None:
                    print("[worker] revision led to no viable cart — re-proposing the pre-revision cart")
                    current_items = items
                    _plan_and_propose(
                        order_id, items, max_total,
                        cart_version=cart_version,
                        amendment_ids=amendment_ids,
                        revision_instruction=latest["instruction_text"],
                        revision_could_not_be_applied=True,
                        adapters=adapters,
                    )
                seen_amendments = len(amendments)
        elif state in ("approved", "executing", "done", "aborted", "refused"):
            print(f"[worker] order reached terminal/human-owned state: {state} — stopping")
            return

        time.sleep(POLL_SECONDS)


# sku -> the request phrase it answers, from the most recent proposal. The next
# revision needs it to connect "remove onion" to the list entry that produced
# the onion; it is rebuilt from disk when the worker starts cold.
_LAST_LINE_QUERIES: dict[str, str] = {}

# query -> why the model said nothing in the results answered it. Kept so the
# narrative can tell the human WHY an item is absent instead of simply omitting
# it: "I could not find amul butter — the store only had cookies and
# buttermilk" is an answer; a missing line is not.
_MODEL_REFUSALS: dict[str, str] = {}

# search phrase -> {merchant: [Listing]}, from earlier rounds of THIS order.
#
# WHY A REVISION SHOULD NOT RE-SHOP THE WHOLE BASKET. Changing the atta does not
# change what eggs cost, and searching five merchants' catalogues again to find
# that out is a minute of someone's time and five merchants' patience for no
# information. Gourab, on his third revision: "you should only run and get the
# items that user has asked to change not the entire cart."
#
# KEYED ON THE UNDERSTOOD SEARCH PHRASE, NOT THE LIST ENTRY — which is what
# makes it work for the revisions people actually send. The understanding layer
# strips the quantity, so "pilsbury multigrain atta 5kg" and "2kg pilsbury
# multigrain atta" are both the phrase "pillsbury multigrain atta". A quantity
# change, a brand swap and a rejected product therefore all reuse the listings
# already in hand and only re-decide; nothing is searched again. Only a genuinely
# new thing — a replace_item or an add — reaches a merchant.
#
# WHAT IS STILL RECOMPUTED EVERY TIME, and must be: selection, pack counts,
# landed cost and the merchant ranking. One item changing can change which store
# wins the whole basket, so re-ranking is not optional — it is just free.
#
# PERSISTED TO DISK, PER ORDER — and it has to be, which was learned the hard
# way. This cache was in-process only, on the reasoning that a restarted worker
# should go and look again. That is tidy and it is wrong about how the thing is
# actually used: the human types a revision into the Console and THEN starts a
# worker, so the process handling round two is almost never the process that did
# round one. The cache was empty every single time, every revision re-shopped
# the whole basket, and the optimisation existed only in the tests. Observed on
# order_f884a496f8d7: a revision that added butter went straight back out to
# re-search "Get 2 kg potato".
#
# BOUNDED BY AGE, because the reason for caution was real even if the conclusion
# was not. These are live prices; past _CACHE_TTL_SECONDS they are discarded and
# re-read. Fifteen minutes is longer than a revision round and far shorter than
# quick-commerce prices move.
_CACHE_TTL_SECONDS = 15 * 60

_LISTING_CACHE: dict[str, dict[str, list]] = {}
_LISTING_CACHE_ORDER: str | None = None


def _cache_path(order_id: str) -> Path:
    return config.DATA / "listing-cache" / f"{order_id}.json"


def _listing_to_dict(listing: Listing) -> dict:
    row = asdict(listing)
    # raw_description is untrusted merchant prose and nothing downstream of the
    # cache reads it. Not persisting it keeps the file small and keeps the one
    # attacker-controlled field out of one more place.
    row.pop("raw_description", None)
    return row


def reset_listing_cache(order_id: str) -> None:
    """Load this order's cached listings, or start clean for a different order."""
    global _LISTING_CACHE_ORDER
    if _LISTING_CACHE_ORDER == order_id:
        return
    _LISTING_CACHE.clear()
    _LISTING_CACHE_ORDER = order_id
    try:
        blob = json.loads(_cache_path(order_id).read_text())
    except (OSError, json.JSONDecodeError):
        return
    if time.time() - float(blob.get("saved_at", 0)) > _CACHE_TTL_SECONDS:
        print("[worker] cached prices for this order are stale — re-reading them")
        return
    for phrase, by_merchant in (blob.get("phrases") or {}).items():
        try:
            _LISTING_CACHE[phrase] = {
                merchant: [Listing(**row) for row in rows]
                for merchant, rows in by_merchant.items()
            }
        except TypeError:
            # The Listing shape changed since this was written. Drop it rather
            # than half-load it: a cache is never worth a wrong price.
            _LISTING_CACHE.clear()
            return
    if _LISTING_CACHE:
        print(f"[worker] {len(_LISTING_CACHE)} item(s) already priced for this order")


def _save_listing_cache(order_id: str) -> None:
    try:
        path = _cache_path(order_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "saved_at": time.time(),
            "phrases": {
                phrase: {m: [_listing_to_dict(l) for l in ls] for m, ls in by_merchant.items()}
                for phrase, by_merchant in _LISTING_CACHE.items()
            },
        }))
        path.chmod(0o600)
    except (OSError, TypeError) as exc:
        print(f"[worker] could not save the price cache ({exc}) — the next round re-reads")


def _record_revision(order_id: str, instruction: str, plan, applied: dict,
                     before: list[str]) -> None:
    """Every revision leaves a trace, including the ops that were thrown away.

    A model that invents an item to modify must not be able to do it quietly —
    `reviser_output_rejected` is the record that makes a bad op visible rather
    than merely ineffective."""
    for dropped in plan.rejected:
        tools.record_decision(
            trace_id=order_id, actor="cart-reviser", action="reviser_output_rejected",
            reasoning=f"refused an op from the interpreter: {dropped['why']}",
            evidence=[{"raw_op": dropped["raw"]}], confidence="high",
        )
    if plan.unavailable:
        tools.record_decision(
            trace_id=order_id, actor="cart-reviser", action="revision_interpreter_unavailable",
            reasoning=f"could not read {instruction!r}: {plan.unavailable_reason}. The cart is "
                      "unchanged — this never falls back to pattern matching, because a wrong "
                      "reading that looks like a right one is what emptied a cart before.",
            confidence="high",
        )
        return
    tools.record_decision(
        trace_id=order_id, actor="cart-reviser", action="revision_interpreted",
        reasoning=f"{instruction!r} -> "
                  + ("; ".join(f"{o.op} {o.target or o.value!r}" for o in plan.ops) or "no change")
                  + (f"; list {before} -> {applied['items']}" if applied["items"] != before else ""),
        evidence=[{"ops": [{"op": o.op, "target": o.target, "value": o.value} for o in plan.ops]}],
        confidence="high" if plan.actionable else "medium",
    )


def _requested_quantity(understood: dict | None, query: str):
    """How much was asked for, preferring the model's reading of the line.

    parse_quantity_phrase anchors on a LEADING number, so it returns None for
    "pilsbury multigrain atta 5kg" and "a dozen eggs" — and a None here means
    qty=1, which is how a 5 kg request bought a 10 kg pack. The model reads the
    amount wherever it sits in the sentence and resolves "a dozen". Its answer
    is converted through the same normalise vocabulary as everything else, so a
    unit it does not recognise falls back rather than inventing a conversion.
    """
    if understood and understood.get("quantity_value") and understood.get("quantity_unit"):
        unit = str(understood["quantity_unit"])
        value = float(understood["quantity_value"])
        if unit == "count":
            return value, "count"
        base_kind, multiplier = normalise_mod._parse_unit(unit)
        if base_kind != "unknown":
            return value * multiplier, base_kind
    return normalise_mod.parse_quantity_phrase(query)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m worker.run <order_id>")
        raise SystemExit(1)
    main(sys.argv[1])
