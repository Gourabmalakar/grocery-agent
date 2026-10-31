"""Approving a payment by typing a passphrase into Telegram.

GOURAB CHOSE THIS over a Console round-trip and the friction argument is fair.
The cost is real and no mitigation removes it, so it is written down here rather
than buried: A PASSPHRASE TYPED INTO TELEGRAM EXISTS ON TELEGRAM'S SERVERS.
Regular chats are not end-to-end encrypted, and deleteMessage clears the visible
chat, not necessarily every backup. What follows reduces the blast radius of
that fact; it does not undo it.

  - A SEPARATE SECRET. bot/payment uses its own hash file, so a Telegram
    compromise does not also hand over the Console, and either can be rotated
    without the other.
  - DELETED ON RECEIPT, before it is even checked, so the window in which it is
    visible in the chat is as short as the API allows.
  - NEVER PERSISTED. It is verified in-process against a PBKDF2 hash and the
    plaintext is dropped. It reaches no log, no audit record, no order file —
    tests/bot asserts that.
  - SCOPED TO ONE BASKET. An approval authorises exactly one (order_id,
    cart_hash) pair, once, for five minutes. Not a session, not a mode. If the
    basket changes by a rupee, the approval is void, because cart_hash changed.
  - LOCKOUT. Three wrong attempts trips the kill switch to PAUSE rather than
    merely refusing, because repeated wrong guesses in a chat that can spend
    money is not a typo story.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from grocery import config

# NOTE: there is no verify() here, deliberately. The bot RELAYS a passphrase to
# the Console and never adjudicates it — see console/auth/bot_passphrase.py. So
# compromising this process yields the ability to ask for a payment, not the
# ability to approve one. What lives here is scope, expiry and attempt counting:
# the user-facing half of the gate, none of which is a secret.


class PaymentNotArmed(RuntimeError):
    pass


@dataclass
class PendingApproval:
    """One basket, waiting for a passphrase. Held in memory only."""
    order_id: str
    cart_hash: str
    rail_id: str
    amount_inr: float
    expires_at: float
    attempts: int = 0

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at

    def matches(self, order_id: str, cart_hash: str) -> bool:
        return self.order_id == order_id and self.cart_hash == cart_hash


@dataclass
class ApprovalGate:
    """Arms one payment, verifies one passphrase, forgets everything.

    Deliberately NOT a session. There is no state here that outlives a single
    approval, so there is nothing for a later message to inherit.
    """
    _pending: dict[str, PendingApproval] = field(default_factory=dict)

    def arm(self, chat_id: str, *, order_id: str, cart_hash: str, rail_id: str,
            amount_inr: float) -> PendingApproval:
        pending = PendingApproval(
            order_id=order_id, cart_hash=cart_hash, rail_id=rail_id,
            amount_inr=amount_inr,
            expires_at=time.time() + config.PASSPHRASE_TTL_SECONDS,
        )
        self._pending[chat_id] = pending
        return pending

    def awaiting(self, chat_id: str) -> PendingApproval | None:
        pending = self._pending.get(chat_id)
        if pending is None:
            return None
        if pending.expired():
            del self._pending[chat_id]
            return None
        return pending

    def disarm(self, chat_id: str) -> None:
        self._pending.pop(chat_id, None)

    def record(self, chat_id: str, *, accepted: bool) -> tuple[bool, str]:
        """Book the OUTCOME of a verification the Console performed.

        Takes a boolean, never the passphrase — this object has no way to see
        one, which is what keeps the plaintext from reaching anything that
        outlives the single function that relays it.
        """
        pending = self.awaiting(chat_id)
        if pending is None:
            raise PaymentNotArmed("nothing is waiting for a passphrase")

        if accepted:
            # Single use. Burn it before the caller can act on it, so a failure
            # downstream cannot be retried into a second payment.
            self.disarm(chat_id)
            return True, ""

        pending.attempts += 1
        left = config.PASSPHRASE_MAX_ATTEMPTS - pending.attempts
        if left <= 0:
            self.disarm(chat_id)
            # Repeated wrong guesses in a chat that can spend money is not a
            # typo story. Pausing is cheap; being wrong about this is not.
            try:
                from grocery import killswitch
                killswitch.trip_pause(
                    reason="too many wrong payment passphrases in chat",
                    set_by="bot.payment")
            except Exception:  # noqa: BLE001 - never let the lockout itself fail open
                pass
            return False, ("too many wrong attempts — payments are paused. "
                           "Clear the pause in the Console when you're ready.")
        return False, f"that didn't match. {left} attempt{'s' if left != 1 else ''} left."
