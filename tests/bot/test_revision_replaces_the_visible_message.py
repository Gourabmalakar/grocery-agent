"""A revised basket must land where he is looking.

order_b194b618e78d, 2026-10-12: a revision completed correctly — cart_version 2,
narrative rewritten, Console showing the new basket — and the chat sat on
"redoing the basket…" indefinitely. Nothing had failed. The finished basket
edited a message that had scrolled out of view, because the edit anchor was
still the FIRST "checking…" message from when the order was created.

A result delivered somewhere the human is not looking is indistinguishable from
no result, and reads as a hang.
"""
from __future__ import annotations

import pytest


def _order(version: int, state: str = "proposed"):
    return {"order_id": "order_x", "state": state, "cart_version": version,
            "cart": {"merchant": "swiggy", "landed_total_inr": 648.0, "line_items": []},
            "comparison": [{"merchant": "swiggy", "landed_total_inr": 648.0,
                            "items_fulfilled": 1, "items_requested": 1,
                            "line_items": [{"name": "Amul Butter", "qty": 1,
                                            "line_total_inr": 648.0}]}]}


def test_a_revision_releases_the_edit_anchor(isolated_data_dir, monkeypatch):
    """The one-line fix, asserted directly: after a revision the next basket
    must be free to attach to the new progress message."""
    from bot import console_client, session
    from bot.conversation import Conversation

    monkeypatch.setattr(session.config, "TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(console_client, "revise", lambda oid, text: (True, ""))

    conversation = Conversation()
    state = conversation.state_for("chat")
    state.order_id = "order_x"
    state.status_message_id = 11          # the original "checking…" message
    monkeypatch.setattr(conversation, "_spawn_worker", lambda s: None)

    conversation._revise(state, "add cadbury silk")

    assert state.status_message_id is None, (
        "the revised basket would have edited the message from before the "
        "revision, which is where it was NOT visible")
    assert state.shown_cart_version == -1


def test_the_revised_basket_edits_the_newest_progress_message(isolated_data_dir, monkeypatch):
    """End to end through the same sequence the bug took: first basket, then a
    revision, then the revised basket."""
    from bot import console_client, session
    from bot.channel import Incoming
    from bot.conversation import Conversation

    monkeypatch.setattr(session.config, "TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(console_client, "revise", lambda oid, text: (True, ""))
    monkeypatch.setattr(console_client, "get_order", lambda oid: _order(_version[0]))

    _version = [1]
    conversation = Conversation()
    monkeypatch.setattr(conversation, "_spawn_worker", lambda s: None)
    state = conversation.state_for("chat")
    state.order_id, state.status_message_id, state.shown_cart_version = "order_x", 11, 0

    # The first basket edits the original "checking…" message. Correct.
    first = conversation.poll_orders()
    assert first[0][1].edit_message_id == 11

    # He asks for a change; the progress message is a NEW message, id 12.
    conversation.handle(Incoming(chat_id="chat", text="add cadbury silk"))
    assert state.status_message_id is None
    state.status_message_id = 12          # run.py adopts it, because it is None

    _version[0] = 2
    revised = conversation.poll_orders()
    assert revised, "the revised basket never reached the chat"
    assert revised[0][1].edit_message_id == 12, (
        "the revised basket edited the pre-revision message again")
