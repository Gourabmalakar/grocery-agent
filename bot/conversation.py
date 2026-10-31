"""What the bot does when Gourab says something.

One active order per chat. The state lives in memory on purpose: it is a
pointer to an order, and the order itself is the durable thing on disk. A
restarted bot forgets which order a chat was looking at, which is recoverable
(`/status`), where a restarted bot inventing an order would not be.

THIS MODULE NEVER IMPORTS bot/telegram.py. It takes a ChatChannel and returns
Outgoing messages, so the WhatsApp swap the plan describes is a swap.
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from bot import console_client, payment, format as fmt, session
from bot.channel import Incoming, Outgoing
from grocery import config

_REPO = Path(__file__).resolve().parent.parent

def _split_into_items(text: str) -> list[str]:
    """A sentence into a shopping list.

    Splitting on commas is where this used to stop, and it is not where item
    boundaries are: "buy 5kg atta and 2kg rice" has no comma, became one item,
    was searched verbatim at four merchants and matched nothing
    (order_80bd9e8f2635). The model reads it properly; the comma split stays as
    the fallback for when the model is off, because a bad split still beats
    refusing to shop.
    """
    from worker.llm import shopping as llm_shopping

    understood = llm_shopping.split_request(text)
    if understood:
        return understood
    return [part.strip() for part in text.split(",") if part.strip()]


HELP = (
    "🧺 <b>Grocery Butler</b>\n\n"
    "Send me a shopping list — <code>milk 1l, 30 eggs, amul butter 250g</code> — "
    "and I'll price it across the stores.\n\n"
    "While a basket is on screen, just tell me what to change: "
    "<i>\"2kg atta instead of the bread\"</i>, <i>\"drop the eggs\"</i>.\n\n"
    "/new — start a fresh list\n"
    "/status — where the current order is\n"
    "/cancel — abandon it"
)


@dataclass
class ChatState:
    order_id: str | None = None
    status_message_id: int | None = None
    shown_cart_version: int = 0
    worker: subprocess.Popen | None = field(default=None, repr=False)
    # The last state we told the human about, so a change announced once is not
    # announced again on every poll tick.
    notified_state: str = ""
    last_items: list[str] = field(default_factory=list)
    # The basket a payment would be for: its hash scopes the approval, so a
    # passphrase typed about one cart cannot pay for a different one.
    pay_cart_hash: str = ""
    pay_amount_inr: float = 0.0
    pay_method: str = ""


class Conversation:
    def __init__(self, max_total_inr: int | None = None) -> None:
        self._chats: dict[str, ChatState] = {}
        self._cap = max_total_inr or config.CAP_PER_ORDER_INR
        # Holds scope, expiry and attempt counts for a payment awaiting a
        # passphrase. It never sees a passphrase — see bot/payment.py.
        self._gate = payment.ApprovalGate()
        # Set by bot/run.py to the channel's delete(). Left None in tests and
        # anywhere the channel is absent, where _delete() is a no-op.
        self.delete_message = None

    def state_for(self, chat_id: str) -> ChatState:
        return self._chats.setdefault(chat_id, ChatState())

    # ---- inbound -------------------------------------------------------

    def handle(self, incoming: Incoming) -> list[Outgoing]:
        if not session.is_allowed(incoming.chat_id):
            session.note_rejection(incoming.chat_id, incoming.text or incoming.data)
            return []
        if incoming.is_button:
            return self._on_button(incoming)
        return self._on_text(incoming)

    def _on_text(self, incoming: Incoming) -> list[Outgoing]:
        state = self.state_for(incoming.chat_id)
        text = incoming.text.strip()
        if not text:
            return []

        # A PASSPHRASE IS INTERCEPTED FIRST, BEFORE ANY OTHER INTERPRETATION.
        # If this sat lower down, a passphrase that happened to look like a
        # grocery list would start an order — and the plaintext would then flow
        # into an intent mandate, the decision log and the order file, which is
        # exactly where it must never go. It is also deleted from the chat
        # before it is checked, so the window where it is visible is as short
        # as the API allows.
        if self._gate.awaiting(incoming.chat_id) is not None:
            return self._on_passphrase(incoming, text)

        lowered = text.lower()
        if lowered in ("/start", "/help"):
            return [Outgoing(HELP)]
        if lowered == "/status":
            return [self._status(state)]
        if lowered in ("/cancel", "/abort"):
            return [self._cancel(state)]
        if lowered == "/new":
            state.order_id, state.status_message_id, state.shown_cart_version = None, None, 0
            return [Outgoing("Fresh start — what are we buying?")]

        # A message while a basket is on screen is a revision, not a new order.
        # Getting this the wrong way round would silently abandon a cart the
        # human was in the middle of correcting.
        if state.order_id and self._is_revisable(state):
            return self._revise(state, text)
        return self._start_order(state, text)

    def _on_button(self, incoming: Incoming) -> list[Outgoing]:
        state = self.state_for(incoming.chat_id)
        action, _, argument = incoming.data.partition(":")

        if action == "pay":
            if not state.order_id:
                return [Outgoing("That basket has expired — send the list again.")]
            if argument == "app":
                # Nothing to authorise: this rail cannot charge anything, so
                # asking for a passphrase would be theatre.
                return self._settle(state, method="app", passphrase="")
            if argument in ("wallet", "cod"):
                # ARMED, NOT PAID. The next message is expected to be the
                # passphrase; nothing moves until the Console verifies it.
                # COD takes a passphrase too. No instrument is charged, but a
                # real order is committed and a real debt incurred — "nobody
                # typed anything and a rider turned up" is not an acceptable
                # outcome either.
                self._gate.arm(incoming.chat_id, order_id=state.order_id,
                               cart_hash=state.pay_cart_hash,
                               rail_id=f"swiggy_{argument}",
                               amount_inr=state.pay_amount_inr)
                state.pay_method = argument
                return [fmt.ask_passphrase(state.pay_amount_inr, method=argument)]
            return [Outgoing(f"I don't know how to pay by {argument!r}.")]

        if action == "demo":
            # FOR SHOW. Gourab keeps Zepto on screen while presenting and wants
            # nothing to happen when it is pressed. Returning no messages is
            # literally that: Telegram acknowledges the tap, the chat does not
            # move, and no state changes.
            #
            # The safety property is unchanged and is the reason this branch
            # exists at all rather than simply routing Zepto to "take": it
            # reaches neither choose_basket, approve_cart nor build_cart. A
            # button that looks buyable and silently does nothing is a demo
            # prop; one that looks buyable and half-commits an order is the bug
            # that cost a day. This is the first, and a test holds it there.
            return []

        if action == "take":
            # ONE TAP = CHOOSE THIS STORE AND APPROVE IT. Both baskets are
            # shown in full, so by the time he taps he has already compared
            # them; making him pick and then confirm would be asking the same
            # question twice. Approving a cart moves no money — payment is a
            # separate gate that this button cannot reach.
            if not state.order_id:
                return [Outgoing("That basket has expired — send the list again.")]
            order = console_client.get_order(state.order_id)
            current = (order.get("cart") or {}).get("merchant", "")
            if argument != current:
                ok, detail = console_client.choose_basket(state.order_id, argument)
                if not ok:
                    return [Outgoing(f"Couldn't switch to {argument}: {detail}")]
            ok, detail = console_client.approve_cart(state.order_id)
            if not ok:
                return [Outgoing(f"Couldn't approve that: {detail}")]
            state.shown_cart_version = -1
            state.notified_state = "approved"

            # Approving freezes OUR record of the basket. Building puts it in
            # his actual merchant account, which is the thing he can act on.
            # It spends nothing — the cart surface has no checkout tool — so it
            # follows approval directly rather than asking a second time.
            order = console_client.get_order(state.order_id)
            ok, built = console_client.build_cart(state.order_id)
            if ok and isinstance(built, dict):
                state.notified_state = "built"
                messages = [fmt.built(order, built)]
                methods = console_client.payment_methods()
                if methods:
                    state.pay_cart_hash = str((order.get("cart") or {}).get("cart_hash", ""))
                    state.pay_amount_inr = float(built.get("total_inr") or 0)
                    messages.append(fmt.pay_choice(
                        dict(built, order_id=state.order_id), methods))
                return messages
            return [fmt.approved(order),
                    Outgoing(f"<i>I couldn't build the basket at the store: "
                             f"{fmt.escape(str(built))[:200]}</i>")]

        if action == "abort":
            return [self._cancel(state)]
        return []

    # ---- order lifecycle -----------------------------------------------

    def _start_order(self, state: ChatState, text: str) -> list[Outgoing]:
        items = _split_into_items(text)
        if not items:
            return [Outgoing("I didn't catch any items in that.")]
        try:
            order_id = console_client.create_order(items, self._cap)
        except Exception as exc:                     # noqa: BLE001
            return [Outgoing(f"Couldn't start that order: {str(exc)[:200]}")]

        state.order_id = order_id
        state.shown_cart_version = 0
        state.status_message_id = None
        state.notified_state = ""
        state.last_items = items
        self._spawn_worker(state)
        return [fmt.searching(items)]

    def _on_passphrase(self, incoming: Incoming, text: str) -> list[Outgoing]:
        """Relay one passphrase and forget it.

        `text` is a parameter and nothing else. It is not stored on the state,
        not logged, not put in an exception message, and not echoed back — the
        only thing that survives this method is a boolean.
        """
        state = self.state_for(incoming.chat_id)
        pending = self._gate.awaiting(incoming.chat_id)
        if pending is None:                           # expired between checks
            return [Outgoing("That approval expired. Tap the pay button again.")]

        # Delete BEFORE verifying, so a slow verification does not lengthen the
        # time it is sitting in the chat.
        self._delete(incoming)

        if not pending.matches(state.order_id or "", state.pay_cart_hash):
            self._gate.disarm(incoming.chat_id)
            return [Outgoing("The basket changed since I asked — look at it "
                             "again before paying.")]

        ok, result = console_client.pay(
            pending.order_id, method=state.pay_method or "wallet", passphrase=text,
            cart_hash=pending.cart_hash,
        )
        del text                                      # not kept a moment longer

        if not ok and result == "passphrase":
            accepted, reason = self._gate.record(incoming.chat_id, accepted=False)
            return [Outgoing(reason)]

        self._gate.record(incoming.chat_id, accepted=True)
        if not ok:
            detail = result if isinstance(result, str) else str(result)
            if "unknown" in detail.lower():
                return [fmt.payment_uncertain(detail)]
            return [Outgoing(f"Payment refused: {detail}")]

        state.notified_state = "done"
        return [fmt.paid(result if isinstance(result, dict) else {})]

    def _settle(self, state: ChatState, *, method: str, passphrase: str) -> list[Outgoing]:
        """The hand-off path. Charges nothing, so it needs no passphrase."""
        ok, result = console_client.pay(
            state.order_id, method=method, passphrase=passphrase,
            cart_hash=state.pay_cart_hash,
        )
        if not ok:
            detail = result if isinstance(result, str) else str(result)
            return [Outgoing(f"Couldn't record that: {detail}")]
        state.notified_state = "awaiting_payment"
        return [fmt.handed_off(result if isinstance(result, dict) else {})]

    def _delete(self, incoming: Incoming) -> None:
        """Best effort, and its failure is never fatal — but it is the single
        most important line in the passphrase path, so it is not buried inside
        a larger method."""
        deleter = getattr(self, "delete_message", None)
        if deleter is None:
            return
        try:
            deleter(incoming.chat_id, incoming.message_id)
        except Exception:                             # noqa: BLE001
            pass

    def _revise(self, state: ChatState, text: str) -> list[Outgoing]:
        ok, detail = console_client.revise(state.order_id, text)
        if not ok:
            return [Outgoing(f"Couldn't apply that: {detail}")]
        state.shown_cart_version = -1

        # THE REVISED BASKET MUST REPLACE THE MESSAGE HE IS LOOKING AT, not the
        # one he was looking at ten minutes ago.
        #
        # `status_message_id` is the message the finished basket EDITS in place,
        # so a 30-90s run does not stack six progress messages. It is set once,
        # from the first "checking…" message, and run.py only adopts a new one
        # when it is None. Without this line it therefore still pointed at that
        # original message — which the FIRST proposal had already edited and
        # which had long since scrolled away.
        #
        # So a revision sent "redoing the basket…" to the bottom of the chat and
        # then quietly rewrote a basket far above it. The last thing visible
        # stayed "redoing the basket…" forever, while the Console showed the new
        # cart — exactly the "it updates in the console but not in telegram"
        # that order_b194b618e78d looked like. Clearing it hands the anchor to
        # the progress message we are about to send, which is what
        # _start_order() has always done for a new order.
        state.status_message_id = None

        self._spawn_worker(state)
        return [Outgoing(f"✏️ <i>{text[:120]}</i> — redoing the basket…")]

    def _spawn_worker(self, state: ChatState) -> None:
        """The missing seam. Safe for the bot to do because the worker has no
        authority at all — worker/tools.py is four functions and none of them
        can approve anything."""
        if state.worker and state.worker.poll() is None:
            return                                   # one is already running
        state.worker = subprocess.Popen(
            [sys.executable, "-u", "-m", "worker.run", state.order_id],
            cwd=_REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _cancel(self, state: ChatState) -> Outgoing:
        if not state.order_id:
            return Outgoing("Nothing to cancel.")
        console_client.abort(state.order_id)
        if state.worker and state.worker.poll() is None:
            state.worker.terminate()
        state.order_id, state.status_message_id, state.shown_cart_version = None, None, 0
        return Outgoing("Cancelled.")

    def _status(self, state: ChatState) -> Outgoing:
        if not state.order_id:
            return Outgoing("No order on the go. Send me a list.")
        order = console_client.get_order(state.order_id)
        return Outgoing(f"<b>{order['state']}</b> · v{order.get('cart_version', 1)}\n"
                        f'<a href="{fmt.order_url(state.order_id)}">Open in Console →</a>')

    def _is_revisable(self, state: ChatState) -> bool:
        try:
            return console_client.get_order(state.order_id)["state"] in ("proposed", "revising")
        except Exception:                            # noqa: BLE001
            return False

    # ---- outbound, driven by the order changing ------------------------

    def poll_orders(self) -> list[tuple[str, Outgoing]]:
        """Called on every loop tick. Returns (chat_id, message) for any order
        that has moved since the human last saw it."""
        out: list[tuple[str, Outgoing]] = []
        for chat_id, state in self._chats.items():
            if not state.order_id:
                continue
            try:
                order = console_client.get_order(state.order_id)
            except Exception:                        # noqa: BLE001
                continue

            version = order.get("cart_version", 1)
            status = order["state"]

            if status == "proposed" and order.get("cart") and version != state.shown_cart_version:
                # No pre-display switching any more. The message shows every
                # buyable basket whatever the ranking preferred, and the store
                # is chosen by the tap — so silently moving the order before he
                # has seen it would be deciding something he is about to decide.
                state.shown_cart_version = version
                message = fmt.proposal(order)
                # Edit the "checking…" message in place rather than stacking a
                # new one: a live run takes 30-90 seconds and a progress log
                # made of six messages is worse than none.
                out.append((chat_id, Outgoing(message.text, message.buttons,
                                              state.status_message_id)))

            # THE CHAT FOLLOWS THE ORDER, WHEREVER IT IS CHANGED FROM. Gourab
            # approved a cart in the Console and the chat said nothing, because
            # this only ever watched for `proposed`. The Console is not going
            # away — it is the inspection surface and still has buttons — so
            # every state the order can reach has to reach the chat too.
            elif (status == "approved"
                  # ALREADY HANDLED IN THE CHAT. Building a cart does not change
                  # the ORDER's state — it stays "approved" — so a poll tick
                  # right after a successful build saw "approved", found
                  # notified_state=="built", and sent the generic "cart approved,
                  # paying is switched off by design" message underneath the
                  # basket that had just been handed over. Two messages, the
                  # second contradicting the first and both wrong about
                  # payments, which are now on.
                  and state.notified_state not in ("approved", "built",
                                                   "awaiting_payment", "done")):
                state.notified_state = "approved"
                out.append((chat_id, fmt.approved(order)))
            elif status in ("executing", "done") and state.notified_state != status:
                state.notified_state = status
                out.append((chat_id, Outgoing(f"Order {status}.")))
            # NOTHING FOUND, AND NOBODY TOLD HIM. A first proposal that finds
            # no viable cart leaves the order in `draft` and returns None — the
            # worker then polls quietly until its 15-minute wall clock and the
            # chat says nothing at all. Gourab waited 3½ minutes on an order
            # that had already failed after 8 seconds (order_80bd9e8f2635).
            #
            # The worker having EXITED is the signal: it only stops when it has
            # nothing left to do, so draft-with-no-cart plus a dead worker means
            # it tried and could not.
            elif (status in ("draft", "revising") and not order.get("cart")
                  and state.worker is not None and state.worker.poll() is not None
                  and state.notified_state != "dead_end"):
                state.notified_state = "dead_end"
                out.append((chat_id, Outgoing(
                    "😕 I couldn't find any of that at the stores I can buy from.\n\n"
                    f"<i>{fmt.escape(', '.join(state.last_items)[:200])}</i>\n\n"
                    "Try naming a brand, or fewer things at once — and the reasons "
                    f'are all here:\n<a href="{fmt.order_url(state.order_id)}">'
                    f'what I tried →</a>')))
                state.order_id = None

            elif status in ("aborted", "refused") and state.notified_state != status:
                state.notified_state = status
                # "Order refused." on its own, underneath a refusal that had
                # already been explained, told him nothing and left the most
                # important fact unsaid: no money moved. A terminal state is
                # exactly when to say what happened to the money and what to do
                # next, because the basket is gone and he has to decide.
                if status == "refused":
                    out.append((chat_id, Outgoing(
                        "That basket is closed and <b>nothing was charged</b>. "
                        "Send the list again to start a fresh one.")))
                else:
                    out.append((chat_id, Outgoing(
                        "Cancelled — nothing was charged.")))
                state.order_id = None
        return out
