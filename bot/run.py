"""The bot process. `python -m bot.run`

Single-threaded on purpose. It alternates between a short Telegram long-poll
and a check of whatever orders are in flight, which keeps the whole thing one
readable loop with no shared state to get wrong. The cost is that an order
update can take up to POLL_SECONDS to appear; the benefit is that there is no
second thread able to send a message about an order the first thread just
cancelled.

It holds no payment authority. Its Console credential (BOT_API_TOKEN) is
accepted on three non-money routes and refused on approve_payment.
"""
from __future__ import annotations

import sys
import time

from bot.channel import Outgoing
from bot.conversation import Conversation
from bot.telegram import TelegramChannel
from grocery import config

# Short enough that a finished basket appears promptly, long enough that we are
# not hammering Telegram. Telegram holds the connection for this long when
# there is nothing to say, so this is not a busy loop.
POLL_SECONDS = 5


def main() -> int:
    if not config.TELEGRAM_BOT_TOKEN:
        print("no TELEGRAM_BOT_TOKEN set — nothing to run")
        return 1
    if not config.TELEGRAM_CHAT_ID:
        # Refuse rather than default to trusting whoever messages first.
        print("no TELEGRAM_CHAT_ID set — refusing to start without an allowlist")
        return 1

    channel = TelegramChannel(config.TELEGRAM_BOT_TOKEN)
    conversation = Conversation()
    # So a passphrase can be removed from the chat the instant it arrives.
    conversation.delete_message = channel.delete
    print(f"[bot] listening · merchants={config.MERCHANT_BACKEND} "
          f"· cap=₹{config.CAP_PER_ORDER_INR} · pay={config.PAYMENT_BACKEND} "
          f"· console={config.CONSOLE_URL}")

    while True:
        try:
            for incoming in channel.poll(timeout_seconds=POLL_SECONDS):
                if incoming.callback_id:
                    channel.acknowledge(incoming.callback_id)
                for reply in conversation.handle(incoming):
                    message_id = channel.send(incoming.chat_id, reply)
                    # Remember the "checking…" message so the finished basket
                    # can replace it instead of stacking underneath.
                    state = conversation.state_for(incoming.chat_id)
                    if state.order_id and state.status_message_id is None:
                        state.status_message_id = message_id

            for chat_id, message in conversation.poll_orders():
                sent = channel.send(chat_id, message)
                state = conversation.state_for(chat_id)
                if message.edit_message_id is None:
                    state.status_message_id = sent

        except KeyboardInterrupt:
            print("\n[bot] stopped")
            return 0
        except Exception as exc:  # noqa: BLE001 — one bad update must not end the bot
            print(f"[bot] loop error: {str(exc)[:200]}")
            time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
