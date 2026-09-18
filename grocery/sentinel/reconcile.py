"""
Reconciliation — comparing independent records of the same fact against each
other. Split out from checks.py as its own module because reconciliation is
conceptually different from the other checks: it doesn't ask "is this thing
broken", it asks "do these two things, which should always agree, actually
agree" — and it's useful on its own (e.g. from a REPL after an incident,
before deciding whether to trust either side).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from grocery import config
from grocery.ledger import store as ledger_store


@dataclass
class ReconciliationResult:
    orders_total_inr: float
    orders_count: int
    ledger_total_inr: float
    ledger_count: int

    @property
    def matches(self) -> bool:
        return abs(self.orders_total_inr - self.ledger_total_inr) <= 0.01

    @property
    def discrepancy_inr(self) -> float:
        return round(self.orders_total_inr - self.ledger_total_inr, 2)


def reconcile_orders_vs_ledger() -> ReconciliationResult:
    """The two sources of truth for "how much money has actually moved":
    every order file's own recorded `payment.amount_inr` for orders in the
    `done` state, and the SQLite spend ledger that budget checks are made
    against. They are written by different code paths at different times
    (see console/approval.py's approve_payment_and_execute) precisely so
    that a bug in one is unlikely to also be a bug in the other — a mismatch
    here is exactly the kind of thing a single log could never catch on its
    own."""
    from console import approval  # see checks.py's note on this import direction

    done = [o for o in approval.list_orders() if o.state.value == "done" and o.payment]
    orders_total = sum(o.payment["amount_inr"] for o in done)

    with sqlite3.connect(str(config.LEDGER_DB)) as conn:
        conn.execute(ledger_store._SCHEMA)
        row = conn.execute("SELECT COALESCE(SUM(amount_inr), 0) AS s, COUNT(*) AS c FROM spend").fetchone()
        ledger_total, ledger_count = float(row[0]), int(row[1])

    return ReconciliationResult(
        orders_total_inr=orders_total, orders_count=len(done),
        ledger_total_inr=ledger_total, ledger_count=ledger_count,
    )
