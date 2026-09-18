"""
A rolling behavioural baseline — orders/day, average order value, merchants
ever seen — that check_behavioral_drift compares fresh activity against, per
the plan's "slow-onset rogue behaviour that no single transaction reveals"
check class.

Deliberately a single small JSON file rather than a database: a human should
be able to open this and read it directly during an incident, and it needs
no schema migration story at this scale (a personal grocery agent, not a
fleet). Bounded to the most recent 200 orders so it can't grow unbounded.

Only `done` orders with a real payment feed the baseline — a proposed or
aborted cart says nothing about actual spending behaviour.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from grocery import config

_BASELINE_PATH = config.DATA / "sentinel" / "baseline.json"
_BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load() -> dict[str, Any]:
    if not _BASELINE_PATH.exists():
        return {"orders_seen": []}
    try:
        return json.loads(_BASELINE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        # A corrupt baseline file should never crash the sentinel — it just
        # means drift detection starts cold again, which is safe (fails
        # toward "not enough history to say", never toward silence about a
        # real anomaly).
        return {"orders_seen": []}


def _save(state: dict[str, Any]) -> None:
    _BASELINE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def record_order(*, order_id: str, amount_inr: float, merchant: str, ts: float) -> None:
    """Idempotent — call this every time a sweep sees a `done` order; an
    order already recorded is a no-op rather than a duplicate."""
    state = _load()
    if any(o["order_id"] == order_id for o in state["orders_seen"]):
        return
    state["orders_seen"].append(
        {"order_id": order_id, "amount_inr": amount_inr, "merchant": merchant, "ts": ts}
    )
    state["orders_seen"] = state["orders_seen"][-200:]
    _save(state)


def summary(*, exclude_order_id: str | None = None) -> dict[str, Any]:
    """Mean/stdev of order value and average orders/day, excluding one order
    id if given — so a fresh order can never inflate the very baseline it is
    about to be compared against."""
    rows = [o for o in _load()["orders_seen"] if o["order_id"] != exclude_order_id]
    if not rows:
        return {"n": 0, "mean_amount_inr": 0.0, "stdev_amount_inr": 0.0,
                "mean_orders_per_day": 0.0, "merchants_seen": []}

    amounts = [r["amount_inr"] for r in rows]
    per_day: dict[str, int] = {}
    for r in rows:
        day = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        per_day[day] = per_day.get(day, 0) + 1

    return {
        "n": len(rows),
        "mean_amount_inr": statistics.mean(amounts),
        "stdev_amount_inr": statistics.pstdev(amounts) if len(amounts) > 1 else 0.0,
        "mean_orders_per_day": statistics.mean(per_day.values()) if per_day else 0.0,
        "merchants_seen": sorted({r["merchant"] for r in rows}),
    }


def reset() -> None:
    """Testing/administrative escape hatch — not exposed to the worker or
    any HTTP route."""
    _save({"orders_seen": []})
