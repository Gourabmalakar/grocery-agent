"""What the bot can and cannot do.

The bot holds a standing credential, so the question "what does that credential
buy an attacker" has to have a short, checkable answer. It is: create an order,
revise a basket, approve a CART. Never approve a payment.

These run offline — no Telegram, no network beyond the local Console.
"""
from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

_REPO = Path(__file__).resolve().parents[2]


# ---- the inbound allowlist ---------------------------------------------

def test_only_the_allowlisted_chat_is_answered(isolated_data_dir, monkeypatch):
    from bot import session

    monkeypatch.setattr(session.config, "TELEGRAM_CHAT_ID", "100200300")
    assert session.is_allowed("100200300")
    assert not session.is_allowed("999")
    assert not session.is_allowed("")


def test_no_allowlist_means_nobody(isolated_data_dir, monkeypatch):
    """An unset allowlist must not mean "trust whoever arrives first" — the bot
    is discoverable by username to anyone on Telegram."""
    from bot import session

    monkeypatch.setattr(session.config, "TELEGRAM_CHAT_ID", "")
    assert not session.is_allowed("100200300")


def test_a_stranger_is_audited_not_merely_ignored(isolated_data_dir, monkeypatch):
    from bot import session
    from grocery.audit import log as audit_log

    monkeypatch.setattr(session, "_SEEN_STRANGERS", {})
    session.note_rejection("4242", "hello?")
    events = [e for e in audit_log.read_all() if e.get("event") == "bot.message_from_unknown_chat"]
    assert events and events[-1]["detail"]["chat_id"] == "4242"


def test_a_strangers_message_is_not_copied_wholesale_into_the_audit_chain(isolated_data_dir, monkeypatch):
    from bot import session
    from grocery.audit import log as audit_log

    monkeypatch.setattr(session, "_SEEN_STRANGERS", {})
    session.note_rejection("4243", "x" * 500)
    events = [e for e in audit_log.read_all() if e.get("event") == "bot.message_from_unknown_chat"]
    assert len(events[-1]["detail"]["text_prefix"]) <= 40


def test_the_conversation_refuses_an_unknown_chat(isolated_data_dir, monkeypatch):
    from bot import session
    from bot.channel import Incoming
    from bot.conversation import Conversation

    monkeypatch.setattr(session.config, "TELEGRAM_CHAT_ID", "100200300")
    assert Conversation().handle(Incoming(chat_id="999", text="milk, eggs")) == []


# ---- the outbound credential -------------------------------------------

def test_the_bot_token_cannot_approve_a_payment(live, monkeypatch):
    """THE boundary. The bot can freeze a cart; it can never buy one."""
    from console import approval
    from grocery.mandates import cart as cart_m, intent as intent_m

    token = "test-bot-token"   # matches what the live fixture gives the Console

    intent = intent_m.new_intent(items=["milk"], max_total_inr=800)
    intent_m.persist(intent)
    order = approval.create_order(intent)
    cart = cart_m.new_cart(
        intent_id=intent["intent_id"], merchant="dailygrocer",
        line_items=[{"sku": "dg-milk-1l", "qty": 1, "intent_ref": intent["intent_id"],
                     "price_source": "test", "asof": 0, "price_inr": 50.0}],
        fees={"delivery": 0, "handling": 0, "surge": 0, "coupon": 0}, landed_total_inr=50.0)
    cart_m.persist(cart)
    approval.propose_cart(order.order_id, cart)

    client = httpx.Client(base_url=live["console"], timeout=10)
    headers = {"X-Bot-Token": token}

    # It may approve the cart…
    assert client.post(f"/console/order/{order.order_id}/approve_cart",
                       headers=headers).status_code == 200
    # …and must not be able to pay for it.
    paid = client.post(f"/console/order/{order.order_id}/approve_payment", headers=headers)
    assert paid.status_code in (401, 403), paid.text

    still = client.get(f"/api/order/{order.order_id}").json()
    assert still["state"] == "approved", "the bot moved an order past approval"
    assert not still.get("payment")


def test_a_wrong_bot_token_is_refused(live):
    client = httpx.Client(base_url=live["console"], timeout=10)
    r = client.post("/console/order/order_nope/approve_cart",
                    headers={"X-Bot-Token": "not-the-token"})
    assert r.status_code == 401


def test_an_empty_header_never_authorises(live):
    """An empty token must not compare equal to anything, configured or not."""
    client = httpx.Client(base_url=live["console"], timeout=10)
    assert client.post("/console/order/x/approve_cart",
                       headers={"X-Bot-Token": ""}).status_code == 401


# ---- the bot stays outside the money boundary ---------------------------

def test_the_bot_never_imports_the_approval_machinery(isolated_data_dir):
    """Same discipline as worker/tools.py: the bot talks HTTP to the Console
    and must not be able to reach into it."""
    forbidden = ("console.approval", "console.rails", "grocery.ledger",
                 "grocery.mandates.payment")
    offenders = []
    for path in sorted((_REPO / "bot").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            module = (node.module if isinstance(node, ast.ImportFrom)
                      else node.names[0].name if isinstance(node, ast.Import) else None)
            if module and any(module.startswith(f) for f in forbidden):
                offenders.append(f"{path.name}:{node.lineno} -> {module}")
    assert not offenders, offenders


def test_the_credentials_never_reach_an_audit_record(isolated_data_dir, monkeypatch):
    from bot import session
    from grocery.audit import log as audit_log

    monkeypatch.setattr(session.config, "TELEGRAM_BOT_TOKEN", "1234567890:SECRETPART")
    monkeypatch.setattr(session, "_SEEN_STRANGERS", {})
    session.note_rejection("1", "hi")
    blob = "".join(str(e) for e in audit_log.read_all())
    assert "SECRETPART" not in blob


def test_the_token_is_scrubbed_from_transport_errors(isolated_data_dir):
    """A requests error quotes the URL it failed on, and the token is in the
    URL path — that is how Telegram's API is shaped and cannot be changed."""
    from bot.telegram import TelegramChannel

    channel = TelegramChannel("1234567890:SECRETPART")
    assert "SECRETPART" not in channel._redact(
        "HTTPSConnectionPool(host='api.telegram.org'): /bot1234567890:SECRETPART/getUpdates")
