"""Who is allowed to talk to this bot. The answer is one person.

An allowlist of a single chat id is the bot's entire inbound authentication,
and it has to be taken seriously for a reason that is easy to miss: anyone who
can get a message into this conversation can create orders, revise baskets and
approve carts. Telegram will happily deliver a message from any stranger who
finds the bot's username.

Rejections are AUDITED rather than merely dropped. A stranger messaging a
personal shopping bot is either a wrong number or the first move of something
worse, and the difference is only visible if there is a record of it.
"""
from __future__ import annotations

import time

from grocery import config
from grocery.audit import log as audit_log

_SEEN_STRANGERS: dict[str, float] = {}
_RENOTICE_SECONDS = 3600


def is_allowed(chat_id: str) -> bool:
    allowed = (config.TELEGRAM_CHAT_ID or "").strip()
    if not allowed:
        # No allowlist configured means the bot is open to the world. Refuse
        # everything rather than default to trusting whoever arrives first.
        return False
    return str(chat_id).strip() == allowed


def note_rejection(chat_id: str, text: str) -> None:
    """Audited once an hour per stranger — enough to see it happened, not
    enough to let someone fill the audit log by spamming."""
    now = time.time()
    if now - _SEEN_STRANGERS.get(chat_id, 0) < _RENOTICE_SECONDS:
        return
    _SEEN_STRANGERS[chat_id] = now
    audit_log.append("bot.message_from_unknown_chat", {
        "chat_id": chat_id,
        # The first few characters only: enough to tell a wrong number from a
        # probe, without copying a stranger's message into our audit chain.
        "text_prefix": str(text)[:40],
    })
    print(f"[bot] ignored a message from unknown chat {chat_id}")
