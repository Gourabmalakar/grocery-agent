"""What a chat channel has to be able to do, independent of which chat it is.

Telegram first; WhatsApp needs a Meta business account and template approval
before it can send anything, so it is a later swap rather than a parallel build.
Keeping the logic behind this protocol is what makes that a swap instead of a
rewrite — `bot/conversation.py` never imports `bot/telegram.py`.

The four operations are chosen from what the conversation actually needs:

  send      a new message, optionally with buttons
  edit      replace a message's text IN PLACE — a live merchant run takes
            30-90 seconds, and a progress bar made of six new messages is
            worse than no progress bar at all
  delete    remove a message the user sent. Only used for one thing, and it is
            the important one: the approval passphrase, which must not sit in
            the chat history (Milestone 3)
  acknowledge  answer a button press so the client stops showing a spinner
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Incoming:
    """One thing the human did. `data` is set when a button was pressed rather
    than text typed; the two are deliberately the same type, because the
    conversation cares about intent, not about which widget produced it."""
    chat_id: str
    text: str = ""
    data: str = ""                 # callback_data from a button
    message_id: int | None = None  # the user's own message, for delete
    callback_id: str = ""          # needed to acknowledge a button

    @property
    def is_button(self) -> bool:
        return bool(self.data)


@dataclass(frozen=True)
class Button:
    label: str
    data: str   # Telegram caps callback_data at 64 BYTES — see telegram.py


@dataclass(frozen=True)
class Outgoing:
    text: str
    buttons: list[Button] = field(default_factory=list)
    edit_message_id: int | None = None   # edit in place instead of sending new


class ChatChannel(Protocol):
    def poll(self, *, timeout_seconds: int) -> list[Incoming]: ...
    def send(self, chat_id: str, message: Outgoing) -> int | None: ...
    def delete(self, chat_id: str, message_id: int) -> None: ...
    def acknowledge(self, callback_id: str, note: str = "") -> None: ...
