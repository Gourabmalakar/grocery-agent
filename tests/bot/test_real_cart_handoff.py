"""Approving in chat puts a real basket in the merchant's account.

Approval freezes OUR record of a cart. Building puts it in his actual Zepto
account, which is the thing he can act on — and it spends nothing, because the
UCP cart surface has no checkout tool and no order tool. That is why it follows
approval directly instead of asking a second time.
"""
from __future__ import annotations

import pytest


def _approved_order():
    return {"order_id": "order_x", "state": "approved",
            "cart": {"merchant": "zepto", "landed_total_inr": 633.0, "line_items": []}}


def _built():
    return {"merchant": "zepto", "cart_ref": "cart_abc", "total_inr": 633.0,
            "fees_known": False,
            "continue_url": "https://www.zepto.com/cart?id=cart_abc",
            "lines": [{"name": "Amul Salted Butter", "qty": 1}]}


def test_approving_builds_the_real_cart_and_hands_over_the_link(isolated_data_dir, monkeypatch):
    from bot import console_client, session
    from bot.channel import Incoming
    from bot.conversation import Conversation

    monkeypatch.setattr(session.config, "TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(console_client, "get_order", lambda oid: _approved_order())
    monkeypatch.setattr(console_client, "approve_cart", lambda oid: (True, ""))
    monkeypatch.setattr(console_client, "build_cart", lambda oid: (True, _built()))

    conversation = Conversation()
    conversation.state_for("chat").order_id = "order_x"
    replies = conversation.handle(Incoming(chat_id="chat", data="take:zepto"))

    text = replies[0].text
    assert "In your zepto cart" in text
    assert "zepto.com/cart?id=cart_abc" in text
    assert "Nothing has been paid" in text


def _built_swiggy():
    return {"merchant": "swiggy", "cart_ref": "11cece9a", "total_inr": 451.0,
            "fees_known": True, "continue_url": "",
            "lines": [{"name": "Bajaj Astor Power Strip", "qty": 1}]}


def test_a_swiggy_cart_is_the_app_cart_and_says_so(isolated_data_dir):
    """MEASURED: Swiggy keys its cart by selectedAddressId, so it is
    account-scoped. Confirmed against a screenshot of his phone — same two
    items, same cartId, same ₹451. Here, "open the app" is true."""
    from bot import format as fmt

    text = fmt.built(_approved_order(), _built_swiggy()).text
    assert "Open the swiggy app" in text
    assert "same cart your phone is showing" in text
    assert "won't show in the swiggy app" not in text


def test_a_zepto_cart_is_not_the_app_cart_and_says_so(isolated_data_dir):
    """THE BUG THAT COST A DAY, now asserted in the other direction.

    Zepto's update_cart requires `deviceId` — "a fallback cart key when a
    transportSessionId is not available" — and "persists cart state for the
    session". The cart belongs to whichever device built it. This agent is not
    his phone, so a Zepto basket can NEVER appear in the Zepto app.

    The old message told him to open the app and pull to refresh. He did, found
    an empty cart, and concluded the system was broken. It was not. Pointing a
    human at a place his items cannot be is worse than saying nothing, because
    an empty cart there reads as proof the build failed."""
    from bot import format as fmt

    text = fmt.built(_approved_order(), _built()).text
    assert "Open the zepto app" not in text
    assert "pull to refresh" not in text
    assert "won't show in the zepto app" in text
    assert "keys its cart to the device that built it" in text
    # The link is the ONLY way to reach this basket, so it must be there.
    assert "zepto.com/cart?id=cart_abc" in text


def test_a_device_scoped_cart_with_no_link_admits_it_is_unreachable(isolated_data_dir):
    """No continue_url and not in the app means the basket exists nowhere he
    can get to. Saying so is the only honest option."""
    from bot import format as fmt

    text = fmt.built(_approved_order(), dict(_built(), continue_url="")).text
    assert "no way to reach this basket" in text


def test_a_cart_stage_total_says_it_excludes_fees(isolated_data_dir):
    """This is the screen he acts on, so the caveat matters more here than
    anywhere else in the conversation."""
    from bot import format as fmt

    text = fmt.built(_approved_order(), _built()).text
    assert "delivery and GST are added at checkout" in text


def test_a_total_that_really_includes_fees_drops_the_caveat(isolated_data_dir):
    from bot import format as fmt

    full = dict(_built(), fees_known=True, total_inr=690.0)
    assert "added at checkout" not in fmt.built(_approved_order(), full).text


def test_a_failed_build_still_confirms_the_approval(isolated_data_dir, monkeypatch):
    """The approval happened and is real; only the merchant-side basket did
    not. Saying nothing, or implying the approval failed too, would both be
    wrong."""
    from bot import console_client, session
    from bot.channel import Incoming
    from bot.conversation import Conversation

    monkeypatch.setattr(session.config, "TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(console_client, "get_order", lambda oid: _approved_order())
    monkeypatch.setattr(console_client, "approve_cart", lambda oid: (True, ""))
    monkeypatch.setattr(console_client, "build_cart",
                        lambda oid: (False, "it may have gone out of stock"))

    conversation = Conversation()
    conversation.state_for("chat").order_id = "order_x"
    replies = conversation.handle(Incoming(chat_id="chat", data="take:zepto"))

    joined = " ".join(r.text for r in replies)
    assert "Cart approved" in joined
    assert "out of stock" in joined
