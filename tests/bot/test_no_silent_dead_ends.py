"""An order that fails must say so. Silence is the worst outcome.

order_80bd9e8f2635: the request became one unsearchable item, every merchant
came back empty, `no_viable_cart` was recorded 8 SECONDS in — and the chat said
nothing for three and a half minutes while the worker sat in its poll loop
waiting out a 15-minute wall clock. Gourab read that as the system being slow.
It had already failed.

Two separate defects, tested separately: the request should not have become one
item, and the failure should not have been silent.
"""
from __future__ import annotations

import pytest


class _DeadWorker:
    """A worker process that has exited — the signal that it tried and stopped."""
    def poll(self):
        return 1


class _LiveWorker:
    def poll(self):
        return None


def _order(state="draft", cart=None):
    return {"order_id": "order_x", "state": state, "cart_version": 1, "cart": cart,
            "comparison": []}


def test_a_dead_worker_with_no_cart_is_reported(isolated_data_dir, monkeypatch):
    from bot import console_client
    from bot.conversation import Conversation

    monkeypatch.setattr(console_client, "get_order", lambda oid: _order("draft"))
    conversation = Conversation()
    state = conversation.state_for("chat")
    state.order_id, state.worker = "order_x", _DeadWorker()
    state.last_items = ["5kg pilusbury multigrain atta", "2kg sona masoori rice"]

    messages = conversation.poll_orders()
    assert messages, "a failed order must produce a message"
    text = messages[0][1].text
    assert "couldn't find" in text.lower()
    assert "sona masoori" in text, "tell him WHAT could not be found"
    assert state.order_id is None, "the order must be released so /new is not needed"


def test_a_still_running_worker_is_not_reported_as_failed(isolated_data_dir, monkeypatch):
    """The whole point of using worker-exited as the signal: a slow search is
    not a failure, and crying failure at 30 seconds would be worse than waiting."""
    from bot import console_client
    from bot.conversation import Conversation

    monkeypatch.setattr(console_client, "get_order", lambda oid: _order("draft"))
    conversation = Conversation()
    state = conversation.state_for("chat")
    state.order_id, state.worker = "order_x", _LiveWorker()
    assert conversation.poll_orders() == []


def test_the_dead_end_is_announced_once(isolated_data_dir, monkeypatch):
    from bot import console_client
    from bot.conversation import Conversation

    monkeypatch.setattr(console_client, "get_order", lambda oid: _order("draft"))
    conversation = Conversation()
    state = conversation.state_for("chat")
    state.order_id, state.worker = "order_x", _DeadWorker()
    state.last_items = ["x"]

    assert len(conversation.poll_orders()) == 1
    assert conversation.poll_orders() == []     # order released, nothing repeated


def test_a_revision_that_finds_nothing_is_also_reported(isolated_data_dir, monkeypatch):
    """Same dead end on the revising path — request_revision nulls the cart, so
    a revision that cannot be fulfilled leaves exactly the same silence."""
    from bot import console_client
    from bot.conversation import Conversation

    monkeypatch.setattr(console_client, "get_order", lambda oid: _order("revising"))
    conversation = Conversation()
    state = conversation.state_for("chat")
    state.order_id, state.worker = "order_x", _DeadWorker()
    state.last_items = ["caviar"]
    assert conversation.poll_orders()


# ---- the splitter, which is why it failed at all -----------------------

def test_items_are_split_by_meaning_not_punctuation(isolated_data_dir, monkeypatch):
    from bot import conversation as conv

    monkeypatch.setattr("worker.llm.shopping.split_request",
                        lambda t: ["5kg pilusbury multigrain atta", "2kg sona masoori rice"])
    assert conv._split_into_items("buy 5kg pilusbury multigrain atta and 2kg sona masoori rice") == [
        "5kg pilusbury multigrain atta", "2kg sona masoori rice"]


def test_the_comma_split_remains_the_fallback(isolated_data_dir, monkeypatch):
    """A bad split still beats refusing to shop when the model is off."""
    from bot import conversation as conv

    monkeypatch.setattr("worker.llm.shopping.split_request", lambda t: None)
    assert conv._split_into_items("milk, eggs, bread") == ["milk", "eggs", "bread"]


def test_the_splitter_does_not_rewrite_the_items(isolated_data_dir):
    """Splitting and correcting are separate steps on purpose — if one step did
    both, an odd result could not be attributed to either."""
    import inspect

    from worker.llm import shopping

    prompt = inspect.getsource(shopping).split("_SPLIT_PROMPT")[1]
    assert "do NOT correct spelling" in prompt.lower() or "not correct spelling" in prompt.lower()
