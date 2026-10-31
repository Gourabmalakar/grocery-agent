"""Chat offers only baskets a human could actually act on.

Gourab, after a basket recommended JioMart: "if you are not able to build real
carts pls dont include the merchant". The worker ranks on fulfilment then price
and knows nothing about who has an API to put things in a basket, so a store
with no cart can win honestly and leave him holding a dead end.

The CONSOLE still shows all four ranked as they really came out — a price you
cannot act on is still worth knowing. This is about what chat offers to approve.
"""
from __future__ import annotations

import pytest


def _comparison():
    return [
        {"merchant": "jiomart", "landed_total_inr": 560.0, "items_fulfilled": 3,
         "items_requested": 4, "line_items": [{"name": "x", "qty": 1, "line_total_inr": 560.0}]},
        # Zepto prices honestly and cannot hand a cart back — see the comment
        # above _UCP_ENDPOINTS in console/ordering.py. It belongs in the
        # comparison and not on a button, exactly like jiomart.
        {"merchant": "zepto", "landed_total_inr": 633.0, "items_fulfilled": 4,
         "items_requested": 4, "line_items": [{"name": "y", "qty": 1, "line_total_inr": 633.0}]},
        {"merchant": "swiggy", "landed_total_inr": 648.0, "items_fulfilled": 4,
         "items_requested": 4, "line_items": [{"name": "y", "qty": 1, "line_total_inr": 648.0}]},
        {"merchant": "blinkit", "landed_total_inr": 700.0, "items_fulfilled": 4,
         "items_requested": 4, "line_items": [{"name": "z", "qty": 1, "line_total_inr": 700.0}]},
    ]


def test_only_cart_capable_stores_are_candidates(isolated_data_dir):
    from bot import format as fmt

    assert [c["merchant"] for c in fmt.cart_capable(_comparison())] == ["swiggy"]


def test_a_dead_end_store_gets_no_button(isolated_data_dir):
    """Offering "Use jiomart" when there is no JioMart cart to build is
    offering a dead end dressed as a choice."""
    from bot import format as fmt

    order = {"order_id": "order_x",
             "cart": {"merchant": "swiggy", "landed_total_inr": 648.0,
                      "line_items": [{"name": "Amul Butter", "qty": 1, "line_total_inr": 130.0}]},
             "comparison": _comparison()}
    labels = " ".join(b.label for b in fmt.proposal(order).buttons)
    assert "jiomart" not in labels and "blinkit" not in labels


def test_the_cheaper_dead_end_is_still_named(isolated_data_dir):
    """Not offering it is not the same as hiding it — he can still go and buy
    it himself, and withholding the number would make the basket look worse
    than the market."""
    from bot import format as fmt

    order = {"order_id": "order_x",
             "cart": {"merchant": "swiggy", "landed_total_inr": 648.0,
                      "line_items": [{"name": "Amul Butter", "qty": 1, "line_total_inr": 130.0}]},
             "comparison": _comparison()}
    text = fmt.proposal(order).text
    assert "jiomart" in text and "560" in text
    assert "no cart I can build" in text


def test_tapping_a_store_selects_and_approves_it(isolated_data_dir, monkeypatch):
    """One tap, because both baskets were shown in full — making him pick and
    then confirm asks the same question twice."""
    from bot import console_client
    from bot.channel import Incoming
    from bot.conversation import Conversation

    calls = []
    monkeypatch.setattr(console_client, "get_order",
                        lambda oid: {"order_id": oid, "state": "proposed",
                                     "cart": {"merchant": "zepto", "landed_total_inr": 633.0,
                                              "line_items": []}})
    monkeypatch.setattr(console_client, "choose_basket",
                        lambda oid, m: (calls.append(("switch", m)), (True, ""))[1])
    monkeypatch.setattr(console_client, "approve_cart",
                        lambda oid: (calls.append(("approve", oid)), (True, ""))[1])
    # Approval now builds the real basket; stub it so this test stays offline.
    monkeypatch.setattr(console_client, "build_cart", lambda oid: (False, "stubbed"))

    conversation = Conversation()
    conversation.state_for("100200300").order_id = "order_x"
    monkeypatch.setattr("bot.session.config.TELEGRAM_CHAT_ID", "100200300")

    conversation.handle(Incoming(chat_id="100200300", data="take:swiggy"))
    assert calls == [("switch", "swiggy"), ("approve", "order_x")]


def test_tapping_the_store_already_selected_does_not_switch(isolated_data_dir, monkeypatch):
    from bot import console_client
    from bot.channel import Incoming
    from bot.conversation import Conversation
    import pytest as _pytest

    monkeypatch.setattr(console_client, "get_order",
                        lambda oid: {"order_id": oid, "state": "proposed",
                                     "cart": {"merchant": "zepto", "landed_total_inr": 633.0,
                                              "line_items": []}})
    monkeypatch.setattr(console_client, "choose_basket",
                        lambda *a, **k: _pytest.fail("already on this store"))
    approved = []
    monkeypatch.setattr(console_client, "approve_cart",
                        lambda oid: (approved.append(oid), (True, ""))[1])
    monkeypatch.setattr(console_client, "build_cart", lambda oid: (False, "stubbed"))

    conversation = Conversation()
    conversation.state_for("100200300").order_id = "order_x"
    monkeypatch.setattr("bot.session.config.TELEGRAM_CHAT_ID", "100200300")
    conversation.handle(Incoming(chat_id="100200300", data="take:zepto"))
    assert approved == ["order_x"]


def test_only_a_buyable_basket_gets_a_take_button(isolated_data_dir):
    """THE DISTINCTION THAT MATTERS, now that Zepto is shown for demos.

    `take:` approves the basket and builds it at the merchant. `demo:` can do
    neither. Zepto may appear on screen — Gourab wants it there while
    presenting — but it must never get a button that approves anything, because
    the original failure was exactly that: a button that looked buyable and
    refused only after he had said yes.
    """
    from bot import format as fmt
    from grocery import config

    order = {"order_id": "order_x",
             "cart": {"merchant": "swiggy", "landed_total_inr": 648.0, "line_items": []},
             "comparison": _comparison()}
    buttons = fmt.proposal(order).buttons
    takes = [b.data for b in buttons if b.data.startswith("take:")]
    demos = [b.data for b in buttons if b.data.startswith("demo:")]

    assert takes == ["take:swiggy"], "something unbuildable got an approve button"
    assert demos == ["demo:zepto"]
    # The merchants with no API at all get nothing of either kind.
    for absent in ("jiomart", "blinkit"):
        assert not any(absent in b.data for b in buttons)
    assert config.CART_CAPABLE_MERCHANTS == {"swiggy"}


def test_the_demo_button_is_indistinguishable_on_screen(isolated_data_dir):
    """Gourab wants Zepto shown during a demo looking like any other option —
    no badge, no "(demo)" label announcing the limitation to his audience. The
    difference lives in the callback data, where only this code sees it."""
    from bot import format as fmt

    order = {"order_id": "order_x",
             "cart": {"merchant": "swiggy", "landed_total_inr": 648.0, "line_items": []},
             "comparison": _comparison()}
    message = fmt.proposal(order)

    assert "demo" not in message.text.lower()
    assert "can't build" not in message.text.lower()
    zepto = next(b for b in message.buttons if b.data == "demo:zepto")
    swiggy = next(b for b in message.buttons if b.data == "take:swiggy")
    assert "demo" not in zepto.label.lower() and "🎭" not in zepto.label
    # Same shape as the real one, differing only by merchant and price.
    assert zepto.label.startswith("✅") and swiggy.label.startswith("✅")


def test_pressing_the_demo_button_does_nothing_at_all(isolated_data_dir, monkeypatch):
    """"nothing will happen if i click on it" — so no reply, and no state
    change. An inert prop, not a message about being a prop."""
    from bot import console_client, session
    from bot.channel import Incoming
    from bot.conversation import Conversation

    monkeypatch.setattr(session.config, "TELEGRAM_CHAT_ID", "chat")

    def _forbidden(*a, **k):
        raise AssertionError("a demo button reached a state-changing call")

    for name in ("approve_cart", "build_cart", "choose_basket", "get_order"):
        monkeypatch.setattr(console_client, name, _forbidden)

    conversation = Conversation()
    conversation.state_for("chat").order_id = "order_x"
    assert conversation.handle(Incoming(chat_id="chat", data="demo:zepto")) == []


def test_no_buyable_basket_says_so_and_still_shows_prices(isolated_data_dir):
    from bot import format as fmt

    order = {"order_id": "order_x",
             "cart": {"merchant": "jiomart", "landed_total_inr": 560.0, "line_items": []},
             # Strip every cart-capable merchant, so nothing is buyable.
             "comparison": [c for c in _comparison()
                            if c["merchant"] not in ("zepto", "swiggy")]}
    message = fmt.proposal(order)
    assert "Nothing I can build a cart at" in message.text
    assert "jiomart" in message.text and "560" in message.text
    assert [b.label for b in message.buttons] == ["✖ Cancel"]
