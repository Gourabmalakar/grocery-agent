"""Paying from chat: two choices, one secret, and what must never happen to it.

Gourab chose a passphrase in chat over a Console round-trip. The cost is that
the passphrase exists on Telegram's servers; these tests hold the line on
everything that is still within our control.
"""
from __future__ import annotations

import pytest


def _order():
    return {"order_id": "order_x", "state": "approved",
            "cart": {"merchant": "swiggy", "landed_total_inr": 451.0,
                     "cart_hash": "HASH1", "line_items": []}}


def _built():
    return {"merchant": "swiggy", "cart_ref": "11cece9a", "total_inr": 451.0,
            "fees_known": True, "continue_url": "", "lines": []}


def _conversation(monkeypatch, methods=("app", "wallet")):
    from bot import console_client, session
    from bot.conversation import Conversation

    monkeypatch.setattr(session.config, "TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(console_client, "get_order", lambda oid: _order())
    monkeypatch.setattr(console_client, "approve_cart", lambda oid: (True, ""))
    monkeypatch.setattr(console_client, "build_cart", lambda oid: (True, _built()))
    monkeypatch.setattr(console_client, "payment_methods", lambda: list(methods))
    return Conversation()


def test_both_payment_choices_are_offered_after_a_basket_is_built(isolated_data_dir, monkeypatch):
    from bot.channel import Incoming

    conversation = _conversation(monkeypatch)
    conversation.state_for("chat").order_id = "order_x"
    replies = conversation.handle(Incoming(chat_id="chat", data="take:swiggy"))

    labels = [b.label for r in replies for b in (r.buttons or [])]
    assert any("pay in the app" in l.lower() for l in labels)
    assert any("wallet" in l.lower() for l in labels)


def test_only_the_handoff_is_offered_when_autonomous_payment_is_off(isolated_data_dir, monkeypatch):
    """PAYMENT_BACKEND=handoff must not show a button that the Console will
    refuse — the two surfaces ask the same source."""
    from bot.channel import Incoming

    conversation = _conversation(monkeypatch, methods=("app",))
    conversation.state_for("chat").order_id = "order_x"
    replies = conversation.handle(Incoming(chat_id="chat", data="take:swiggy"))

    labels = " ".join(b.label for r in replies for b in (r.buttons or [])).lower()
    assert "pay in the app" in labels
    assert "wallet" not in labels


def test_paying_in_the_app_never_asks_for_a_passphrase(isolated_data_dir, monkeypatch):
    """That rail cannot charge anything, so asking would be theatre."""
    from bot import console_client
    from bot.channel import Incoming

    conversation = _conversation(monkeypatch)
    state = conversation.state_for("chat")
    state.order_id, state.pay_cart_hash = "order_x", "HASH1"
    monkeypatch.setattr(console_client, "pay",
                        lambda oid, **kw: (True, {"state": "awaiting_payment",
                                                  "amount_inr": 451.0}))

    replies = conversation.handle(Incoming(chat_id="chat", data="pay:app"))
    text = replies[0].text
    assert "passphrase" not in text.lower()
    assert "nothing has been charged" in text.lower()


def test_the_wallet_button_arms_but_does_not_pay(isolated_data_dir, monkeypatch):
    from bot import console_client
    from bot.channel import Incoming

    conversation = _conversation(monkeypatch)
    state = conversation.state_for("chat")
    state.order_id, state.pay_cart_hash, state.pay_amount_inr = "order_x", "HASH1", 451.0

    def _must_not_pay(*a, **k):
        raise AssertionError("pressing the wallet button paid without a passphrase")

    monkeypatch.setattr(console_client, "pay", _must_not_pay)
    replies = conversation.handle(Incoming(chat_id="chat", data="pay:wallet"))
    assert "passphrase" in replies[0].text.lower()


def test_the_passphrase_is_deleted_before_it_is_verified(isolated_data_dir, monkeypatch):
    """The window in which it sits in the chat must be as short as the API
    allows, so the delete precedes the network call rather than following it."""
    from bot import console_client
    from bot.channel import Incoming

    order_of_events = []
    conversation = _conversation(monkeypatch)
    state = conversation.state_for("chat")
    state.order_id, state.pay_cart_hash, state.pay_amount_inr = "order_x", "HASH1", 451.0

    conversation.delete_message = lambda chat_id, mid: order_of_events.append("deleted")

    def _pay(oid, **kw):
        order_of_events.append("verified")
        return True, {"amount_inr": 451.0, "merchant_order_id": "SW1"}

    monkeypatch.setattr(console_client, "pay", _pay)
    conversation.handle(Incoming(chat_id="chat", data="pay:wallet"))
    conversation.handle(Incoming(chat_id="chat", text="hunter2", message_id=99))

    assert order_of_events == ["deleted", "verified"]


def test_the_passphrase_never_reaches_the_order_or_the_chat_state(isolated_data_dir, monkeypatch):
    """The only thing that may survive the relay is a boolean."""
    from bot import console_client
    from bot.channel import Incoming

    conversation = _conversation(monkeypatch)
    state = conversation.state_for("chat")
    state.order_id, state.pay_cart_hash, state.pay_amount_inr = "order_x", "HASH1", 451.0
    conversation.delete_message = lambda chat_id, mid: None
    monkeypatch.setattr(console_client, "pay",
                        lambda oid, **kw: (True, {"amount_inr": 451.0}))

    conversation.handle(Incoming(chat_id="chat", data="pay:wallet"))
    replies = conversation.handle(Incoming(chat_id="chat", text="hunter2", message_id=99))

    assert "hunter2" not in repr(state)
    assert "hunter2" not in repr(conversation._gate)
    assert all("hunter2" not in r.text for r in replies)


def test_a_passphrase_is_never_read_as_a_grocery_list(isolated_data_dir, monkeypatch):
    """If the interception sat below the revision branch, a passphrase would
    start an order — and the plaintext would land in an intent mandate, the
    decision log and the order file."""
    from bot import console_client
    from bot.channel import Incoming

    conversation = _conversation(monkeypatch)
    state = conversation.state_for("chat")
    state.order_id, state.pay_cart_hash, state.pay_amount_inr = "order_x", "HASH1", 451.0
    conversation.delete_message = lambda chat_id, mid: None

    def _must_not_start(*a, **k):
        raise AssertionError("the passphrase was treated as an order")

    monkeypatch.setattr(console_client, "create_order", _must_not_start)
    monkeypatch.setattr(console_client, "revise", _must_not_start)
    monkeypatch.setattr(console_client, "pay", lambda oid, **kw: (True, {"amount_inr": 1.0}))

    conversation.handle(Incoming(chat_id="chat", data="pay:wallet"))
    conversation.handle(Incoming(chat_id="chat", text="milk bread eggs", message_id=9))


def test_three_wrong_attempts_pause_the_system(isolated_data_dir, monkeypatch):
    """Repeated wrong guesses in a chat that can spend money is not a typo
    story. Pausing is cheap; being wrong about this is not."""
    from bot import console_client
    from bot.channel import Incoming
    from grocery import killswitch

    conversation = _conversation(monkeypatch)
    state = conversation.state_for("chat")
    state.order_id, state.pay_cart_hash, state.pay_amount_inr = "order_x", "HASH1", 451.0
    conversation.delete_message = lambda chat_id, mid: None
    monkeypatch.setattr(console_client, "pay", lambda oid, **kw: (False, "passphrase"))

    conversation.handle(Incoming(chat_id="chat", data="pay:wallet"))
    for _ in range(3):
        conversation.handle(Incoming(chat_id="chat", text="wrong", message_id=1))

    assert killswitch.state().tier is killswitch.Tier.PAUSE


def test_an_uncertain_payment_is_never_reported_as_a_failure(isolated_data_dir, monkeypatch):
    """A failure invites a retry. An unknown must not — that is how one basket
    becomes two orders."""
    from bot import console_client
    from bot.channel import Incoming

    conversation = _conversation(monkeypatch)
    state = conversation.state_for("chat")
    state.order_id, state.pay_cart_hash, state.pay_amount_inr = "order_x", "HASH1", 451.0
    conversation.delete_message = lambda chat_id, mid: None
    monkeypatch.setattr(console_client, "pay", lambda oid, **kw: (
        False, "the Console did not answer in time, so whether the payment went "
               "through is unknown: timeout"))

    conversation.handle(Incoming(chat_id="chat", data="pay:wallet"))
    replies = conversation.handle(Incoming(chat_id="chat", text="right", message_id=1))

    text = replies[0].text
    assert "don't know whether" in text.lower()
    assert "check your swiggy orders" in text.lower()
    assert "won't retry" in text.lower()


def test_cod_is_offered_and_asks_for_a_passphrase(isolated_data_dir, monkeypatch):
    """No instrument is charged, but a real order is committed and a real debt
    incurred — "nobody typed anything and a rider turned up" is not acceptable
    either."""
    from bot import console_client
    from bot.channel import Incoming

    conversation = _conversation(monkeypatch, methods=("app", "cod", "wallet"))
    state = conversation.state_for("chat")
    state.order_id, state.pay_cart_hash, state.pay_amount_inr = "order_x", "HASH1", 102.0

    def _must_not_pay(*a, **k):
        raise AssertionError("COD placed an order without a passphrase")

    monkeypatch.setattr(console_client, "pay", _must_not_pay)
    replies = conversation.handle(Incoming(chat_id="chat", data="pay:cod"))

    text = replies[0].text.lower()
    assert "passphrase" in text
    assert "cash to the rider" in text
    assert state.pay_method == "cod"


def test_a_cod_order_is_not_described_as_paid(isolated_data_dir):
    """Reporting COD as "paid" would be wrong, and he would stop expecting to
    need notes at the door."""
    from bot import format as fmt

    text = fmt.paid({"rail": "swiggy_cod", "amount_inr": 102.0,
                     "merchant_order_id": "SW9"}).text.lower()
    assert "order placed" in text
    assert "pay the rider in cash" in text
    assert "paid ₹102" not in text


def test_the_revision_narrative_reaches_the_chat(isolated_data_dir):
    """order_415b282c5221: the worker explained twice why "add bitter gourd"
    could not be applied, and neither explanation was ever shown. He saw the
    same baskets come back unchanged and concluded revisions were broken."""
    from bot import format as fmt

    order = {
        "order_id": "order_415b282c5221",
        "revision_rounds": 2,
        "narrative": ("I understood what you asked — \"add bitter gourd\" — but "
                      "applying it would have left no items in the cart that any "
                      "store can fulfil. I've kept the cart as it was before this "
                      "change."),
        "cart": {"merchant": "swiggy", "landed_total_inr": 28.0, "line_items": []},
        "comparison": [{"merchant": "swiggy", "landed_total_inr": 28.0,
                        "items_fulfilled": 1, "items_requested": 2,
                        "line_items": [{"name": "Nandini Curd", "qty": 1,
                                        "line_total_inr": 28.0}]}],
    }
    text = fmt.proposal(order).text
    assert "applying it would have left no items" in text
    # Above the baskets, where it answers the question he actually has.
    assert text.index("left no items") < text.index("swiggy")


def test_a_first_proposal_carries_no_narrative_preamble(isolated_data_dir):
    """Only a REVISION needs explaining. Prefixing every first basket with prose
    would bury the thing he asked for."""
    from bot import format as fmt

    order = {"order_id": "order_x", "narrative": "Here is a basket.",
             "cart": {"merchant": "swiggy", "landed_total_inr": 28.0, "line_items": []},
             "comparison": [{"merchant": "swiggy", "landed_total_inr": 28.0,
                             "items_fulfilled": 1, "items_requested": 1,
                             "line_items": [{"name": "Curd", "qty": 1,
                                             "line_total_inr": 28.0}]}]}
    assert "Here is a basket" not in fmt.proposal(order).text
