"""Turning an order into something readable on a phone.

The Console shows all three baskets side by side and every rejected
alternative. A phone cannot, and trying would produce a wall nobody reads — so
this shows THE BUYABLE BASKET and links to the Console for the rest. That is
Gourab's own instruction after the connector research: only one merchant can
actually be bought from, so a three-way comparison in chat is detail without a
decision attached to it.
"""
from __future__ import annotations

from bot.channel import Button, Outgoing
from bot.telegram import escape

__all__ = ['escape']
from grocery import config

# How many lines of a basket to show before collapsing. Chosen so a weekly shop
# still fits beside the totals and the buttons inside Telegram's 4096 chars.
_MAX_LINES_PER_BASKET = 8


def order_url(order_id: str) -> str:
    return f"{config.CONSOLE_URL}/orders/{order_id}"


def searching(items: list[str]) -> Outgoing:
    listed = ", ".join(escape(i) for i in items[:6])
    more = f" (+{len(items) - 6} more)" if len(items) > 6 else ""
    return Outgoing(f"🧺 <b>Shopping for</b> {listed}{more}\n\nChecking the stores…")


def _ranked(comparison: list[dict], merchants) -> list[dict]:
    return sorted(
        (c for c in comparison
         if c.get("merchant", "").lower() in merchants and c.get("line_items")),
        key=lambda c: (-(c.get("items_fulfilled") or 0), c.get("landed_total_inr", 0)),
    )


def cart_capable(comparison: list[dict]) -> list[dict]:
    """The baskets a human could actually act on, cheapest first.

    STRICTLY the ones that can be built. Demo baskets are not in here, and must
    not be — everything that approves, builds or spends keys off this list.
    """
    return _ranked(comparison, config.CART_CAPABLE_MERCHANTS)


def demo_only(comparison: list[dict]) -> list[dict]:
    """Baskets shown for a demo that cannot be built — see config.DEMO_MERCHANTS.

    Kept as a SEPARATE function rather than a flag on cart_capable() so that no
    caller can get a demo basket by accident. The two lists are different kinds
    of thing: one is a set of choices, the other is a set of illustrations.
    """
    return _ranked(comparison, config.DEMO_MERCHANTS - config.CART_CAPABLE_MERCHANTS)


def proposal(order: dict) -> Outgoing:
    """BOTH buyable baskets, in full, with one marked as the recommendation.

    Gourab: "since you can't buy with jiomart, then you need to give users only
    2 options... show both the carts, you can give a cart as recommended, user
    can choose whichever they like."

    So the chat shows every basket a human could actually finish at — today
    Zepto and Swiggy — each with its items and total, and marks the one the
    ranking preferred. The recommendation is a suggestion, not a filter: both
    are one tap away, because the ranking knows fulfilment and price and
    nothing about which shop he trusts or which one delivers fastest tonight.

    Stores with no cart API are not listed as options. The Console still ranks
    all four exactly as they came out — that is the inspection surface, and a
    price you cannot act on is still worth having somewhere.
    """
    cart = order.get("cart") or {}
    comparison = order.get("comparison") or []
    recommended = cart.get("merchant", "")
    options = cart_capable(comparison)
    demos = demo_only(comparison)

    if not options and not demos:
        return _uncartable(order, cart, comparison)

    blocks: list[str] = []
    for entry in options + demos:
        merchant = entry["merchant"]
        star = " ⭐" if merchant == recommended else ""
        header = f"🛒 <b>{escape(merchant)}</b> — ₹{entry['landed_total_inr']:.2f}{star}"
        filled, asked = entry.get("items_fulfilled"), entry.get("items_requested")
        if filled is not None and asked is not None and filled < asked:
            header += f"  ({filled}/{asked} items)"
        lines = [header]

        items = entry.get("line_items") or []
        for item in items[:_MAX_LINES_PER_BASKET]:
            qty = f"{item['qty']} × " if item.get("qty", 1) != 1 else ""
            lines.append(f"  • {qty}{escape(item['name'])[:48]} — ₹{item['line_total_inr']:.2f}")
        if len(items) > _MAX_LINES_PER_BASKET:
            lines.append(f"  • …and {len(items) - _MAX_LINES_PER_BASKET} more")
        missing = entry.get("unfulfilled_items") or []
        if missing:
            lines.append(f"  <i>no {escape(', '.join(missing))[:70]}</i>")
        blocks.append("\n".join(lines))

    text = "\n\n".join(blocks)

    # WHAT HAPPENED TO THE CHANGE HE ASKED FOR. The worker writes a narrative
    # explaining a revision it understood but could not apply — order_415b282c5221
    # said "applying it would have left no items in the cart that any store can
    # fulfil. I've kept the cart as it was." — and nothing ever put that in the
    # chat. He saw the same two baskets come back unchanged, twice, and
    # reasonably concluded revisions were broken. They were not; the answer was
    # written down somewhere he never looked.
    if order.get("revision_rounds"):
        note = str(order.get("narrative") or "").strip()
        if note:
            first = note.split("\n\n")[0].strip()
            text = f"<i>{escape(first[:400])}</i>\n\n" + text

    # A cheaper store we cannot finish at is not an option, but it is still a
    # fact he can act on himself — withholding it would make these baskets look
    # better than the market is.
    shown = {e["merchant"].lower() for e in options + demos}
    floor = min((o["landed_total_inr"] for o in options), default=float("inf"))
    cheaper = [c for c in comparison
               if c.get("merchant", "").lower() not in shown
               and c.get("line_items")
               and c.get("landed_total_inr", 0) < floor]
    if cheaper:
        best = min(cheaper, key=lambda c: c["landed_total_inr"])
        text += (f"\n\n<i>{escape(best['merchant'])} was cheaper at "
                 f"₹{best['landed_total_inr']:.0f}, but has no cart I can build.</i>")

    text += ("\n\n<i>Item prices only — delivery, handling and GST aren't readable "
             "until checkout.</i>")
    text += f'\n<a href="{order_url(order["order_id"])}">Full comparison and reasoning →</a>'

    buttons = []
    for entry in options:
        merchant = entry["merchant"]
        star = " ⭐" if merchant == recommended else ""
        buttons.append(Button(
            f"✅ {merchant} ₹{entry['landed_total_inr']:.0f}{star}",
            f"take:{merchant}",
        ))
    for entry in demos:
        # SHOWN, AND INERT. Gourab wants Zepto on screen during a demo looking
        # like any other option, with nothing happening when it is pressed — so
        # the label carries no badge. The callback is still "demo:", never
        # "take:", and that is what keeps it from approving or building
        # anything; the difference lives in the data, not in the label.
        merchant = entry["merchant"]
        star = " ⭐" if merchant == recommended else ""
        buttons.append(Button(
            f"✅ {merchant} ₹{entry['landed_total_inr']:.0f}{star}",
            f"demo:{merchant}",
        ))
    buttons.append(Button("✖ Cancel", f"abort:{order['order_id']}"))
    return Outgoing(text, buttons=buttons)


def _uncartable(order: dict, cart: dict, comparison: list[dict]) -> Outgoing:
    """Nothing we can finish at — say so plainly and still show the prices,
    because he can shop it himself and that is better than silence."""
    lines = ["😕 <b>Nothing I can build a cart at.</b>",
             "", "Zepto and Swiggy came back empty for this. What the others have:"]
    for entry in sorted(comparison, key=lambda c: c.get("landed_total_inr", 0)):
        if entry.get("line_items"):
            lines.append(f"  • {escape(entry['merchant'])} — ₹{entry['landed_total_inr']:.2f}")
    lines.append(f'\n<a href="{order_url(order["order_id"])}">Full detail →</a>')
    return Outgoing("\n".join(lines),
                    buttons=[Button("✖ Cancel", f"abort:{order['order_id']}")])


def question(order: dict, questions: list[str]) -> Outgoing:
    body = "\n".join(f"❓ {escape(q)}" for q in questions)
    return Outgoing(f"{body}\n\n<i>Just tell me and I'll redo it.</i>")


# WHERE A BUILT CART ACTUALLY LIVES, per merchant. Measured from each
# merchant's own update_cart contract on 2026-10-12, not assumed — and they are
# NOT the same, which is the single thing that made the hand-off unexplainable
# for a day.
#
#   Swiggy:  update_cart(selectedAddressId, items)  required: ["items"]
#            Keyed by the ADDRESS, so the cart is account-scoped. Confirmed
#            against a screenshot of his phone: same two items, same cartId,
#            same ₹451. A basket built here IS the basket in his app.
#
#   Zepto:   update_cart(deviceId, cartItems)       required: ["deviceId", ...]
#            "Unique device identifier used as a fallback cart key when a
#            transportSessionId is not available." … "Persists cart state for
#            the session." Keyed by the DEVICE, so the cart belongs to this
#            agent's session and not to the account. His phone is a different
#            device with a different cart, and nothing built here will EVER
#            appear in the Zepto app. The `continue_url` is the intended
#            hand-off: zepto.com/cart?id=… opened where he is signed in.
#
# Telling a Zepto user to "open the app and pull to refresh" sends him to look
# at a cart that cannot contain his items. That is what this map prevents.
_CART_LIVES_IN_THE_APP = {"swiggy": True, "zepto": False}


def built(order: dict, cart: dict) -> Outgoing:
    """The hand-off, which differs by merchant because the carts differ.

    The first version of this said "open the app" for everyone. That is true
    for Swiggy and false for Zepto, and the false half cost a day of debugging
    — he opened the Zepto app, found an empty cart, and concluded the whole
    system was broken. It was not; the cart was real and in a place the app
    does not look. A hand-off that points somewhere the items cannot be is
    worse than no hand-off, because it reads as proof the build failed.
    """
    merchant = cart.get("merchant", "the store")
    label = escape(merchant)
    in_the_app = _CART_LIVES_IN_THE_APP.get(merchant.lower(), False)

    lines = [f"🧾 <b>In your {label} cart</b> — ₹{cart.get('total_inr', 0):.2f}", ""]
    for line in cart.get("lines", [])[:_MAX_LINES_PER_BASKET]:
        qty = f"{line['qty']} × " if line.get("qty", 1) != 1 else ""
        lines.append(f"  • {qty}{escape(line.get('name', ''))[:48]}")

    lines.append("")
    if in_the_app:
        lines.append(f"<b>Open the {label} app and check out.</b> These items are "
                     f"in your real basket — same cart your phone is showing.")
    else:
        # Say plainly that the app is the wrong place to look, because his
        # instinct will be to look there and find nothing.
        lines.append(f"<b>Check out with the link below</b> — open it where "
                     f"you're signed in to {label}.")
        lines.append(f"<i>This basket won't show in the {label} app: {label} keys "
                     f"its cart to the device that built it, and that's me, not "
                     f"your phone.</i>")

    # WHY THE NUMBER WENT UP. The proposal quotes item prices; the built cart
    # carries the merchant's real bill. On a small basket that gap is large —
    # ₹64 of banana came back as ₹102 — and an unexplained 60% jump between two
    # consecutive messages reads as a bug or a scam, not as delivery and
    # handling. Show the components whenever we actually have them.
    subtotal = float(cart.get("subtotal_inr") or 0)
    fees = float(cart.get("fees_inr") or 0)
    taxes = float(cart.get("taxes_inr") or 0)
    if cart.get("fees_known") and (fees or taxes):
        lines.append("")
        lines.append(f"<i>Items ₹{subtotal:.2f}"
                     + (f" · fees ₹{fees:.2f}" if fees else "")
                     + (f" · tax ₹{taxes:.2f}" if taxes else "")
                     + f" = ₹{float(cart.get('total_inr') or 0):.2f}</i>")
    elif not cart.get("fees_known"):
        lines.append("")
        lines.append("<i>Items only — delivery and GST are added at checkout.</i>")

    if cart.get("continue_url"):
        prompt = "Or open it on the web →" if in_the_app else "Open your basket →"
        lines.append(f'\n<a href="{cart["continue_url"]}">{prompt}</a>')
    elif not in_the_app:
        lines.append("\n<i>No checkout link came back, so there is no way to "
                     "reach this basket — tell me and I'll rebuild it.</i>")

    lines.append("\n<i>Nothing has been paid. Checkout is yours.</i>")
    return Outgoing("\n".join(lines))


def pay_choice(cart: dict, methods: list[str]) -> Outgoing:
    """Offer how to pay for a basket that is already in his account.

    TWO BUTTONS AND THEY ARE NOT EQUIVALENT, so the message says which is
    which rather than leaving the labels to imply it:

      Pay in the app   nothing in this system can charge anything. The basket
                       is in his Swiggy cart and he pays there.
      Pay from wallet  the agent pays, from the Swiggy Money float. Shown only
                       when PAYMENT_BACKEND=wallet, and it asks for a
                       passphrase before anything moves.
    """
    total = cart.get("total_inr", 0)
    lines = [f"💳 <b>How do you want to pay the ₹{total:.2f}?</b>", ""]
    buttons = []
    if "app" in methods:
        lines.append("  • <b>In the app</b> — it's already in your Swiggy basket. "
                     "Nothing here can charge you.")
        buttons.append(Button("📱 I'll pay in the app", "pay:app"))
    if "cod" in methods:
        lines.append("  • <b>Cash on delivery</b> — I place the order now, you pay "
                     "the rider. Nothing is charged to anything.")
        buttons.append(Button(f"💵 Order now, pay ₹{total:.0f} cash", "pay:cod"))
    if "wallet" in methods:
        lines.append("  • <b>Swiggy Money</b> — I pay now from the wallet float. "
                     "I'll ask for your passphrase first.")
        buttons.append(Button(f"🤖 Pay ₹{total:.0f} from wallet", "pay:wallet"))
    if not buttons:
        return Outgoing("Payments are switched off, so the basket is yours to "
                        "pay for in the app.")
    buttons.append(Button("✖ Cancel", "abort:" + str(cart.get("order_id", ""))))
    return Outgoing("\n".join(lines), buttons=buttons)


def ask_passphrase(amount_inr: float, *, method: str = "wallet") -> Outgoing:
    what = ("place the order for <b>₹%.2f</b> payable in cash to the rider" % amount_inr
            if method == "cod"
            else "send <b>₹%.2f</b> from Swiggy Money" % amount_inr)
    return Outgoing(
        f"🔐 Type your payment passphrase to {what}.\n\n<i>I delete the message "
        f"the moment it arrives, and it is never stored. It authorises this one "
        f"basket for 5 minutes.</i>")


def paid(result: dict) -> Outgoing:
    """Says what actually happened, which differs by rail: the wallet rail moved
    money, COD placed an order against cash that has not changed hands yet.
    Reporting COD as "paid" would be wrong and he would stop expecting to need
    notes at the door."""
    order_ref = result.get("merchant_order_id") or ""
    amount = float(result.get("amount_inr") or 0)
    tail = f"\n\nSwiggy order <code>{escape(str(order_ref))}</code>" if order_ref else ""
    if result.get("rail") == "swiggy_cod":
        return Outgoing(
            f"✅ <b>Order placed</b> — ₹{amount:.2f} <b>to pay the rider in "
            f"cash</b>.{tail}\n\n<i>Nothing has been charged to any card or "
            f"wallet. Have the cash ready.</i>")
    return Outgoing(
        f"✅ <b>Paid ₹{amount:.2f}</b> from Swiggy "
        f"Money.{tail}\n\n<i>Track it in the Swiggy app.</i>")


def handed_off(result: dict) -> Outgoing:
    return Outgoing(
        f"📱 <b>Your basket is waiting in the Swiggy app.</b>\n\n"
        f"<i>Nothing has been charged — ₹{float(result.get('amount_inr') or 0):.2f} "
        f"is yours to pay at checkout.</i>")


def payment_uncertain(detail: str) -> Outgoing:
    """The one message that must never be optimistic."""
    return Outgoing(
        f"⚠️ <b>I don't know whether that payment went through.</b>\n\n"
        f"<i>{escape(detail[:300])}</i>\n\n"
        f"<b>Check your Swiggy orders before trying again.</b> I won't retry on "
        f"my own — that is how one basket becomes two orders.")


def approved(order: dict) -> Outgoing:
    cart = order.get("cart") or {}
    # DO NOT HARDCODE "paying is switched off". It said that while
    # PAYMENT_BACKEND=wallet, directly underneath a basket that had just been
    # built, contradicting the message above it and misstating what the system
    # could do. What is true here depends on config, so it is read from config.
    if config.payments_enabled():
        tail = ("Tap a payment option above, or open the merchant's app. "
                "Nothing has been charged.")
    else:
        tail = ("I can't place the order yet — paying is switched off by design. "
                "Open the merchant's app to check out.")
    return Outgoing(
        f"✅ <b>Cart approved</b> — {escape(cart.get('merchant', '?'))}, "
        f"₹{cart.get('landed_total_inr', 0):.2f}\n\n"
        f"{tail} Full record here:\n"
        f'<a href="{order_url(order["order_id"])}">{escape(order["order_id"])}</a>'
    )


def nothing_found(order: dict) -> Outgoing:
    return Outgoing(
        "😕 I couldn't put a basket together for that.\n\n"
        f'<a href="{order_url(order["order_id"])}">What I tried →</a>'
    )
