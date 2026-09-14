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
from datetime import datetime, timezone
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
    day TEXT NOT NULL,     -- YYYY-MM-DD, UTC
    month TEXT NOT NULL    -- YYYY-MM, UTC
);
"""


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(config.LEDGER_DB))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_spend(*, payment_id: str, cart_id: str, amount_inr: float, rail: str) -> None:
    now = time.time()
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    with _conn() as c:
        c.execute(
            "INSERT INTO spend (payment_id, cart_id, amount_inr, rail, executed_at, day, month) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (payment_id, cart_id, amount_inr, rail, now,
             dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m")),
        )


def spend_today(as_of: float | None = None) -> float:
    day = datetime.fromtimestamp(as_of or time.time(), tz=timezone.utc).strftime("%Y-%m-%d")
    with _conn() as c:
        row = c.execute("SELECT COALESCE(SUM(amount_inr), 0) AS s FROM spend WHERE day = ?", (day,)).fetchone()
        return float(row["s"])


def spend_this_month(as_of: float | None = None) -> float:
    month = datetime.fromtimestamp(as_of or time.time(), tz=timezone.utc).strftime("%Y-%m")
    with _conn() as c:
        row = c.execute("SELECT COALESCE(SUM(amount_inr), 0) AS s FROM spend WHERE month = ?", (month,)).fetchone()
        return float(row["s"])


def already_spent(payment_id: str) -> bool:
    """Idempotency guard: has this exact PaymentMandate already been recorded
    as spent? Used to refuse a replayed execute() call — see tests/adversarial."""
    with _conn() as c:
        row = c.execute("SELECT 1 FROM spend WHERE payment_id = ?", (payment_id,)).fetchone()
        return row is not None
