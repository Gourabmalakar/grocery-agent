"""
Turns the structured facts price-scout/cart-comparator already computed into
a warm, first-person message — what a good human shopping assistant would
actually say to you before asking for your OK.

Deterministic and template-based, not an LLM call: every sentence here is
built directly from numbers already in the cart, comparison table, or a
revision instruction — never invented. This is the same discipline as the
rest of Phase 0 (no model in the shopping loop; see CLAUDE.md rule 3, "an
unsourced price is worse than a missing one") applied to tone rather than
arithmetic: a friendlier voice must not become a license to claim something
the data doesn't support.

The detailed reasoning trace (grocery/audit/decisions.py) remains the actual
audit record — this module never replaces it, only adds a plain-English
summary on top. Anyone who wants the full "why" for a specific pick still
has it, unabridged, right below this message on the order page.

A richer, LLM-authored version of this narration is a natural Phase 1+
upgrade — handed these exact facts and just as forbidden from inventing
beyond them.
"""
from __future__ import annotations


def _list_join(items: list[str]) -> str:
    quoted = [f"'{i}'" for i in items]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + " and " + quoted[-1]


def narrate_proposal(
    *,
    cart: dict,
    comparison: list[dict],
    missing_items: list[str],
    not_found_items: list[str],
    max_total_inr: float,
    revision_instruction: str | None = None,
    fees_known: bool = True,
    audit_notes: list[str] | None = None,
    reviser_questions: list[str] | None = None,
) -> str:
    """The main proposal message — used both for a fresh cart and for a cart
    that was just re-planned after feedback (pass revision_instruction to
    get the "done — I've updated it" opening instead of "here's what I
    found")."""
    lines: list[str] = []

    if revision_instruction:
        lines.append(f"Done — I've updated the cart based on \"{revision_instruction.strip()}\". Here's where things stand:")
    else:
        lines.append("Hi! Here's what I found for you:")
    lines.append("")

    for li in cart["line_items"]:
        qty_phrase = f"{li['qty']} x " if li["qty"] != 1 else ""
        # Blinkit's search cards carry no brand field and the adapter refuses
        # to guess one from the name, so brand is legitimately blank there —
        # render "Amul Taaza Toned Milk", not "Amul Taaza Toned Milk ()".
        brand = f" ({li['brand']})" if li.get("brand") else ""
        lines.append(f"  • {qty_phrase}{li['name']}{brand} — ₹{li['line_total_inr']:.2f}")

    lines.append("")
    others = [c for c in comparison if c["merchant"] != cart["merchant"]]

    # A STORE THAT FOUND FEWER THINGS IS NOT A CHEAPER STORE, and saying so is
    # the difference between a comparison and a lie. This block used to compare
    # the winner against the cheapest of every other merchant regardless of what
    # was in their basket, and produced, verbatim: "swiggy was cheapest on item
    # prices at ₹884.00, ahead of blinkit at ₹875.00" — a sentence contradicted
    # by its own two numbers. Blinkit was cheaper because it had not found the
    # oranges. Comparison is with merchants that filled at least as much of the
    # ask; the ones that filled less get their own sentence, because they are
    # genuinely interesting and genuinely not the same offer.
    def _filled(entry: dict) -> int:
        return int(entry.get("items_fulfilled", 0))

    mine = next((c for c in comparison if c["merchant"] == cart["merchant"]), {})
    peers = [c for c in others if _filled(c) >= _filled(mine)]
    fewer = [c for c in others if _filled(c) < _filled(mine)]

    if others and not peers:
        requested = int(mine.get("items_requested", 0))
        found = _filled(mine)
        # "all 7 things" while the audit note two lines below says one of them is
        # missing — both printed in the same message on order_b837482ed7ee. The
        # winner fills the MOST, which is not the same as filling everything.
        how_much = (f"all {requested} things" if found >= requested
                    else f"the most of them ({found} of {requested})")
        lines.append(
            f"I checked {len(comparison)} stores — {cart['merchant']} is the only one that could "
            f"find {how_much}, at ₹{cart['landed_total_inr']:.2f}."
        )
        for entry in sorted(fewer, key=lambda c: c["landed_total_inr"]):
            short = entry.get("unfulfilled_items") or []
            cheaper = " — cheaper, but" if entry["landed_total_inr"] < cart["landed_total_inr"] else " —"
            lines.append(
                f"  • {entry['merchant']} came to ₹{entry['landed_total_inr']:.2f}{cheaper} it had no "
                + (_list_join(short) if short else "everything")
                + "."
            )
    elif peers:
        cheapest_other = min(peers, key=lambda c: c["landed_total_inr"])
        if fees_known:
            lines.append(
                f"I checked {len(comparison)} stores — {cart['merchant']} came out cheapest overall at "
                f"₹{cart['landed_total_inr']:.2f} once delivery and handling are in, ahead of "
                f"{cheapest_other['merchant']} at ₹{cheapest_other['landed_total_inr']:.2f}."
            )
        else:
            # This sentence used to say "once delivery and handling are in"
            # unconditionally, two paragraphs above another that admits the fees
            # could not be read. Both cannot be true, and the confident one was
            # the lie — a real-merchant proposal read as a landed-cost ranking
            # when it is an item-price ranking. Caught by an end-to-end run
            # against live Blinkit and JioMart on 2026-09-27.
            lines.append(
                f"I checked {len(comparison)} stores — {cart['merchant']} was cheapest on item prices "
                f"at ₹{cart['landed_total_inr']:.2f}, ahead of {cheapest_other['merchant']} at "
                f"₹{cheapest_other['landed_total_inr']:.2f}. That is items only — no store's "
                "delivery or handling fee could be read, so this order could invert once they are "
                "added."
            )
    else:
        lines.append(f"{cart['merchant']} was the only store that could put this order together for you.")

    # Named separately even when a like-for-like peer existed: a cheaper store
    # that is missing something is a real option the human may want, and hiding
    # it is the same withholding in the other direction.
    if peers and fewer:
        for entry in sorted(fewer, key=lambda c: c["landed_total_inr"]):
            if entry["landed_total_inr"] >= cart["landed_total_inr"]:
                continue
            short = entry.get("unfulfilled_items") or []
            lines.append(
                f"{entry['merchant']} was cheaper at ₹{entry['landed_total_inr']:.2f}, but could not "
                f"find " + (_list_join(short) if short else "everything you asked for") +
                " — so it is not the same shop."
            )

    if not_found_items:
        lines.append("")
        lines.append(
            "I couldn't find " + _list_join(not_found_items) + " in any store I checked — "
            "you may want to pick " + ("that" if len(not_found_items) == 1 else "those") +
            " up separately, or let me know if there's a different name I should search for."
        )
    if missing_items:
        lines.append("")
        lines.append(
            "I also wasn't able to include " + _list_join(missing_items) + f" from {cart['merchant']} "
            "specifically — happy to source just that item from another store if you'd like."
        )

    lines.append("")
    if fees_known:
        lines.append(
            f"Your total comes to ₹{cart['landed_total_inr']:.2f} "
            f"(₹{cart['fees']['delivery']:.0f} delivery, ₹{cart['fees']['handling']:.0f} handling), "
            f"within your ₹{max_total_inr:.0f} budget."
        )
    else:
        lines.append(
            f"Items come to ₹{cart['landed_total_inr']:.2f}. "
            f"Delivery and handling fees couldn't be read without building a cart on the merchant's "
            f"site — the actual total will be higher by however much they charge. "
            f"Your budget is ₹{max_total_inr:.0f}."
        )
    if reviser_questions:
        # The reviser's question or refusal goes FIRST, above the cart and above
        # the totals. It is the direct answer to what the human just typed, and
        # burying it under a receipt is how "I didn't understand you" turns into
        # a silent re-proposal of the same cart.
        lines.insert(1, "")
        for i, question in enumerate(reviser_questions):
            lines.insert(2 + i, f"  ❓ {question}")

    if audit_notes:
        # THE AUDITOR'S OWN WORDS, ABOVE THE APPROVE BUTTON. These are the places
        # the cart may not answer the request, and burying them under the total
        # is how order_6a45f91aaa2e got an onion and a potato past a human who
        # was reading a confident summary. They are stated before the invitation
        # to approve, not after it.
        lines.append("")
        lines.append("Before you approve, two things I am not sure about:"
                     if len(audit_notes) == 2 else
                     ("Before you approve, something I am not sure about:"
                      if len(audit_notes) == 1 else
                      f"Before you approve, {len(audit_notes)} things I am not sure about:"))
        for note in audit_notes:
            lines.append(f"  • {note}")

    lines.append("")
    lines.append("Let me know if you'd like any changes below, or go ahead and approve the cart whenever you're ready!")
    return "\n".join(lines)


def narrate_revision_could_not_be_applied(*, instruction_text: str) -> str:
    return (
        f"I understood what you asked — \"{instruction_text.strip()}\" — but applying it "
        f"would have left no items in the cart that any store can fulfil. I've kept the "
        f"cart as it was before this change. Could you tell me what you'd like differently?"
    )


def narrate_revision_not_understood(*, instruction_text: str) -> str:
    return (
        f"I wasn't able to act on part of what you asked — \"{instruction_text.strip()}\". "
        f"Right now I can reliably handle things like \"drop the biscuits\" or \"Amul not Britannia\", "
        f"but I don't yet catch quantity corrections or brands and items outside what these stores carry. "
        f"Could you rephrase, or tell me exactly what to change? I've left the cart as it was for now — "
        f"nothing below has changed."
    )


def narrate_payment_prompt(*, cart: dict) -> str:
    return (
        f"Great, the cart's approved! Your total is ₹{cart['landed_total_inr']:.2f} from {cart['merchant']}. "
        f"Whenever you're ready, just confirm payment below and I'll place the order."
    )


def narrate_order_placed(*, order: dict) -> str:
    return (
        f"All done! I've placed your order with {order['cart']['merchant']} — "
        f"₹{order['payment']['amount_inr']:.2f} paid, order reference {order['merchant_order_id']}. "
        f"Thanks for shopping with me today!"
    )


def narrate_aborted() -> str:
    return "No problem — I've cancelled this order. Just start a new request whenever you're ready."


def narrate_refused(*, reason_hint: str = "") -> str:
    base = "I'm sorry, I wasn't able to complete this one"
    if reason_hint:
        return f"{base} — {reason_hint}. Nothing was charged; feel free to start a fresh request."
    return f"{base}. Nothing was charged; feel free to start a fresh request."


def narrate_for_order(order: dict) -> str:
    """Single dispatch point the Console calls to render 'what your
    assistant is saying' regardless of what state the order is in. Prefers
    a narrative the worker already computed and stored (order['narrative'])
    when one exists, since that carries the specific comparison/missing-item
    detail this function alone doesn't have."""
    state = order.get("state")
    if state == "draft":
        return "Give me a moment — I'm out searching the stores for you."
    if state in ("proposed", "revising") and order.get("narrative"):
        return order["narrative"]
    if state == "approved":
        return narrate_payment_prompt(cart=order["cart"])
    if state == "done":
        return narrate_order_placed(order=order)
    if state == "aborted":
        return narrate_aborted()
    if state == "refused":
        return narrate_refused()
    return ""


def narrate_basket_chosen(*, cart: dict, comparison: list[dict]) -> str:
    """The message after a human takes a basket the agent did not recommend.

    It does not argue. The ranking rule knows about fulfilment and price and
    nothing about which shop he trusts or which one is quick to his door today,
    so a human overriding it is information, not an error — and a system that
    answers a choice with a justification of its own preference is one people
    stop telling things to.
    """
    merchant = cart["merchant"]
    mine = next((c for c in comparison if c.get("merchant") == merchant), {})
    filled, asked = mine.get("items_fulfilled"), mine.get("items_requested")

    lines = [f"Done — switched to {merchant}. Here's that basket:", ""]
    for li in cart["line_items"]:
        qty = f"{li['qty']} x " if li.get("qty", 1) != 1 else ""
        brand = f" ({li['brand']})" if li.get("brand") else ""
        lines.append(f"  • {qty}{li['name']}{brand} — ₹{li['line_total_inr']:.2f}")
    lines.append("")
    lines.append(f"Items come to ₹{cart['landed_total_inr']:.2f}.")

    # Said plainly rather than buried, because it is the cost of the choice and
    # he is entitled to make it with the number in front of him.
    if filled is not None and asked is not None and filled < asked:
        short = mine.get("unfulfilled_items") or []
        lines.append(
            f"{merchant} covers {filled} of the {asked} things you asked for"
            + (" — no " + _list_join(short) if short else "") + "."
        )
    lines.append("")
    lines.append("Approve it whenever you're ready, or switch to another store below.")
    return "\n".join(lines)
