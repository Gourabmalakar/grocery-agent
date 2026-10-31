"""
SQLite-backed ledger of executed spend. The thing budget.py checks against.

Deliberately boring: one table, one connection-per-call (SQLite handles this
fine at this scale, and it avoids any shared-connection threading footgun in
a FastAPI + background-worker process pair).
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from grocery import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id TEXT UNIQUE NOT NULL,
    cart_id TEXT NOT NULL,
    amount_inr REAL NOT NULL,
    rail TEXT NOT NULL,
    executed_at REAL NOT NULL,
    day TEXT NOT NULL,     -- YYYY-MM-DD, IST
    month TEXT NOT NULL    -- YYYY-MM, IST
);

-- INTENT TO PAY, WRITTEN BEFORE THE RAIL IS CALLED. The spend table above
-- records what definitely happened; this records what was about to. Without
-- it, a crash between rail.pay() and record_spend() leaves money moved and
-- nothing written down — the order stuck in EXECUTING, the cap unaware, and
-- no way on restart to tell a paid order from an unpaid one.
--
-- `key` is sha256(order_id + cart_hash) and is UNIQUE, which is what makes the
-- replay guard real. The previous guard checked a payment_id minted two lines
-- earlier in the same function, so it could never fire; this one is derived
-- from the order and the exact basket, so the SAME order and basket cannot be
-- paid twice even across processes.
CREATE TABLE IF NOT EXISTS pay_intent (
    key TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    cart_hash TEXT NOT NULL,
    amount_inr REAL NOT NULL,
    rail TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    state TEXT NOT NULL,          -- intent | settled | failed | handed_off
    detail TEXT NOT NULL DEFAULT '',
    merchant_order_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

# India is UTC+5:30 and has no daylight saving, so a fixed offset is exact
# rather than approximate.
#
# THE CAPS USED TO ROLL AT UTC MIDNIGHT, which is 05:30 IST. An order placed at
# 02:00 IST counted against the PREVIOUS day's cap — so the small hours could
# spend a day's budget twice over, once before 05:30 against yesterday and
# again after it against today. A daily cap that resets halfway through the
# night is not a daily cap.
IST = timezone(timedelta(hours=5, minutes=30))


def _ist(as_of: float | None = None) -> datetime:
    return datetime.fromtimestamp(as_of or time.time(), tz=IST)


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(config.LEDGER_DB))
    conn.row_factory = sqlite3.Row
    try:
        # executescript, not execute: _SCHEMA holds more than one statement.
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_spend(*, payment_id: str, cart_id: str, amount_inr: float, rail: str) -> None:
    now = time.time()
    dt = _ist(now)
    with _conn() as c:
        c.execute(
            "INSERT INTO spend (payment_id, cart_id, amount_inr, rail, executed_at, day, month) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (payment_id, cart_id, amount_inr, rail, now,
             dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m")),
        )


def spend_today(as_of: float | None = None) -> float:
    day = _ist(as_of).strftime("%Y-%m-%d")
    with _conn() as c:
        row = c.execute("SELECT COALESCE(SUM(amount_inr), 0) AS s FROM spend WHERE day = ?", (day,)).fetchone()
        return float(row["s"])


def spend_this_month(as_of: float | None = None) -> float:
    month = _ist(as_of).strftime("%Y-%m")
    with _conn() as c:
        row = c.execute("SELECT COALESCE(SUM(amount_inr), 0) AS s FROM spend WHERE month = ?", (month,)).fetchone()
        return float(row["s"])


def already_spent(payment_id: str) -> bool:
    """Idempotency guard: has this exact PaymentMandate already been recorded
    as spent? Used to refuse a replayed execute() call — see tests/adversarial."""
    with _conn() as c:
        row = c.execute("SELECT 1 FROM spend WHERE payment_id = ?", (payment_id,)).fetchone()
        return row is not None


# ---------------------------------------------------------------- intents ---
#
# THE ORDER OF WRITES IS THE WHOLE SAFETY ARGUMENT, so it is worth stating:
#
#     claim_intent()      <- durable, BEFORE any money can move
#     rail.pay()          <- the only step that can be interrupted
#     settle/fail/hand_off()
#
# A crash anywhere leaves a row saying "we were about to pay X for order Y".
# `unsettled()` finds those on restart, and the merchant's own order history —
# which the read grant already covers — says whether it actually happened. The
# previous code called rail.pay() first and recorded afterwards, so an
# interruption was indistinguishable from an order that was never placed.

def intent_key(order_id: str, cart_hash: str) -> str:
    """Derived from the order and the exact basket, NOT from a freshly minted
    payment id. Two execute() calls for the same order and basket produce the
    same key and the second is refused by the PRIMARY KEY, across processes and
    across restarts."""
    import hashlib
    return hashlib.sha256(f"{order_id}:{cart_hash}".encode()).hexdigest()


def claim_intent(*, key: str, order_id: str, cart_hash: str, amount_inr: float,
                 rail: str, payment_id: str) -> tuple[bool, dict | None]:
    """Stake a claim on paying for this order+basket exactly once.

    Returns (claimed, existing). `claimed` is False when somebody — another
    process, an earlier crashed attempt, a double tap — already holds it, and
    `existing` is that row so the caller can say what happened rather than
    guessing.
    """
    now = time.time()
    with _conn() as c:
        row = c.execute("SELECT * FROM pay_intent WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return False, dict(row)
        c.execute(
            "INSERT INTO pay_intent (key, order_id, cart_hash, amount_inr, rail, "
            "payment_id, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'intent', ?, ?)",
            (key, order_id, cart_hash, amount_inr, rail, payment_id, now, now),
        )
    return True, None


def _finish_intent(key: str, state: str, *, detail: str = "",
                   merchant_order_id: str | None = None) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE pay_intent SET state = ?, detail = ?, merchant_order_id = ?, "
            "updated_at = ? WHERE key = ?",
            (state, detail[:500], merchant_order_id, time.time(), key),
        )


def settle_intent(key: str, *, merchant_order_id: str | None, detail: str = "") -> None:
    _finish_intent(key, "settled", detail=detail, merchant_order_id=merchant_order_id)


def fail_intent(key: str, *, detail: str) -> None:
    _finish_intent(key, "failed", detail=detail)


def hand_off_intent(key: str, *, detail: str = "") -> None:
    """The rail did its job and no money moved — a basket was handed to a human
    to pay in the merchant's own app. Distinct from `settled` because nothing
    was charged, and distinct from `failed` because nothing went wrong."""
    _finish_intent(key, "handed_off", detail=detail)


def release_intent(key: str) -> None:
    """Drop a claim that never reached the rail — a refusal between claiming and
    paying. Safe because nothing was sent to the merchant; leaving the row would
    permanently block a legitimate retry of an order that never paid."""
    with _conn() as c:
        c.execute("DELETE FROM pay_intent WHERE key = ? AND state = 'intent'", (key,))


def unsettled() -> list[dict]:
    """Intents that were claimed and never finished — i.e. a crash happened
    while money might have been moving. These need reconciling against the
    merchant's order history before the order is touched again."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM pay_intent WHERE state = 'intent' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def intent_for(key: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM pay_intent WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None
