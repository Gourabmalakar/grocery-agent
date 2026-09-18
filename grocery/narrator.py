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
        lines.append(f"  • {qty_phrase}{li['name']} ({li['brand']}) — ₹{li['line_total_inr']:.2f}")

    lines.append("")
    others = [c for c in comparison if c["merchant"] != cart["merchant"]]
    if others:
        cheapest_other = min(others, key=lambda c: c["landed_total_inr"])
        lines.append(
            f"I checked {len(comparison)} stores — {cart['merchant']} came out cheapest overall at "
            f"₹{cart['landed_total_inr']:.2f} once delivery and handling are in, ahead of "
            f"{cheapest_other['merchant']} at ₹{cheapest_other['landed_total_inr']:.2f}."
        )
    else:
        lines.append(f"{cart['merchant']} was the only store that could put this order together for you.")

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
    lines.append(
        f"Your total comes to ₹{cart['landed_total_inr']:.2f} "
        f"(₹{cart['fees']['delivery']:.0f} delivery, ₹{cart['fees']['handling']:.0f} handling), "
        f"within your ₹{max_total_inr:.0f} budget."
    )
    lines.append("")
    lines.append("Let me know if you'd like any changes below, or go ahead and approve the cart whenever you're ready!")
    return "\n".join(lines)


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
