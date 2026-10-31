"""Telegram transport. Long-polling, never a webhook.

WHY POLLING IS A SECURITY DECISION, not a convenience one. The Console binds
127.0.0.1 and every /api/* route is unauthenticated *because* only loopback can
reach it. A webhook needs a publicly reachable URL, and the moment one exists
that assumption is gone for /api/intent (which creates orders), /health (which
lists pending order ids) and /auth/setup (which is claimable on first run).
Polling reaches outward only: no port opens, no perimeter moves.

THE TOKEN IS A BEARER CREDENTIAL FOR THE WHOLE BOT. It sits in the URL path,
which is how Telegram's API is shaped and is not something we can change — so
_redact() scrubs it from every log line and exception, because a `requests`
error quotes the URL it failed on.
"""
from __future__ import annotations

import html
import time

import requests

from bot.channel import Button, Incoming, Outgoing

_API = "https://api.telegram.org/bot{token}/{method}"

# Telegram's own limits. Exceeding either is a 400, not a truncation.
MAX_MESSAGE_CHARS = 4096
MAX_CALLBACK_BYTES = 64


class TelegramChannel:
    def __init__(self, token: str) -> None:
        self._token = token
        self._offset: int | None = None
        self._session = requests.Session()

    # ---- plumbing ------------------------------------------------------

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<TELEGRAM_BOT_TOKEN>") if self._token else text

    def _call(self, method: str, payload: dict, *, timeout: int = 30) -> dict | None:
        try:
            response = self._session.post(
                _API.format(token=self._token, method=method), json=payload, timeout=timeout
            )
        except requests.RequestException as exc:
            print(f"[bot] {method} unreachable: {self._redact(str(exc))[:160]}")
            return None
        if response.status_code != 200:
            print(f"[bot] {method} HTTP {response.status_code}: "
                  f"{self._redact(response.text)[:200]}")
            return None
        body = response.json()
        if not body.get("ok"):
            print(f"[bot] {method} refused: {self._redact(str(body))[:200]}")
            return None
        return body.get("result")

    # ---- the channel ---------------------------------------------------

    def poll(self, *, timeout_seconds: int = 25) -> list[Incoming]:
        """One long-poll. Telegram holds the connection until something happens
        or the timeout expires, so this is not a busy loop."""
        result = self._call(
            "getUpdates",
            {"offset": self._offset, "timeout": timeout_seconds,
             "allowed_updates": ["message", "callback_query"]},
            timeout=timeout_seconds + 10,
        )
        if not result:
            return []

        out: list[Incoming] = []
        for update in result:
            # Advance PAST this update unconditionally, even if we cannot parse
            # it. An update we never acknowledge is redelivered forever, and a
            # single malformed one would wedge the bot permanently.
            self._offset = update["update_id"] + 1

            if "callback_query" in update:
                query = update["callback_query"]
                out.append(Incoming(
                    chat_id=str(query["message"]["chat"]["id"]),
                    data=query.get("data", ""),
                    message_id=query["message"]["message_id"],
                    callback_id=query.get("id", ""),
                ))
            elif "message" in update and "text" in update["message"]:
                message = update["message"]
                out.append(Incoming(
                    chat_id=str(message["chat"]["id"]),
                    text=message["text"],
                    message_id=message["message_id"],
                ))
        return out

    def send(self, chat_id: str, message: Outgoing) -> int | None:
        payload: dict = {
            "chat_id": chat_id,
            "text": _fit(message.text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if message.buttons:
            payload["reply_markup"] = {"inline_keyboard": _keyboard(message.buttons)}

        if message.edit_message_id is not None:
            payload["message_id"] = message.edit_message_id
            result = self._call("editMessageText", payload)
            # "message is not modified" is a 400 and is harmless — it means the
            # text we computed is the text already on screen.
            return message.edit_message_id if result is None else result.get("message_id")

        result = self._call("sendMessage", payload)
        return result.get("message_id") if result else None

    def delete(self, chat_id: str, message_id: int) -> None:
        """Used for exactly one thing that matters: removing a passphrase the
        human typed, before it sits in the history."""
        self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def acknowledge(self, callback_id: str, note: str = "") -> None:
        if callback_id:
            self._call("answerCallbackQuery",
                       {"callback_query_id": callback_id, "text": note[:200]})


# ---- formatting helpers -------------------------------------------------

def escape(text: str) -> str:
    """Product names contain & and < often enough to matter, and an unescaped
    one makes Telegram reject the whole message as bad HTML."""
    return html.escape(str(text), quote=False)


def _fit(text: str) -> str:
    """Telegram rejects anything over 4096 characters outright — it does not
    truncate for us, it 400s and the human sees nothing at all. Losing the tail
    of a basket is bad; losing the entire reply is worse."""
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    tail = "\n…(trimmed — full detail in the Console)"
    return text[: MAX_MESSAGE_CHARS - len(tail)] + tail


def _keyboard(buttons: list[Button]) -> list[list[dict]]:
    rows = []
    for button in buttons:
        data = button.data.encode("utf-8")[:MAX_CALLBACK_BYTES].decode("utf-8", "ignore")
        rows.append([{"text": button.label, "callback_data": data}])
    return rows
