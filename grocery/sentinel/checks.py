"""
The mechanical half of `transaction-sentinel` (.claude/agents/transaction-
sentinel.md) — pure, read-only functions over raw data (both hash chains,
order files, the ledger, issued mandates), each returning a list of
Finding. Nothing here mutates anything or trips the kill switch itself;
that decision is made by grocery.sentinel.report, kept as a separate step
so every check here stays unit-testable with no side effects (see
tests/sentinel/).

Reads `console.approval` for order state rather than re-parsing
`data/orders/*.json` itself — a deliberate exception to "grocery/ doesn't
import console/": order files are console's data, and duplicating their
parsing here would be exactly the kind of second implementation that drifts
and then disagrees with the first (the project's own rule, borrowed from
the family office's research-table lesson). `transaction-sentinel` is
explicitly allowed to read across the whole system; it just cannot write.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from grocery import config
from grocery.audit import decisions as decision_log
from grocery.audit import verify as audit_verify
from grocery.sentinel import baseline, reconcile

Severity = Literal["info", "warn", "critical", "fatal"]

STUCK_APPROVED_HOURS = 24
STUCK_EXECUTING_MINUTES = 10
ADAPTER_MIN_SAMPLE = 5
ADAPTER_MISS_RATE_THRESHOLD = 0.5


@dataclass
class Finding:
    check: str
    severity: Severity
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"check": self.check, "severity": self.severity, "message": self.message, "evidence": self.evidence}


def _orders():
    # Imported lazily so importing grocery.sentinel.checks never requires
    # console/ to already be importable (e.g. from a bare `grocery` install,
    # or a unit test that only exercises the pure helpers below).
    from console import approval
    return approval.list_orders()


def check_chain_integrity() -> list[Finding]:
    findings = []
    ok, msg = audit_verify.verify_audit_chain()
    if not ok:
        findings.append(Finding("chain_integrity", "fatal", f"audit chain broken: {msg}", {"chain": "audit"}))
    ok, msg = audit_verify.verify_decision_chain()
    if not ok:
        findings.append(Finding("chain_integrity", "fatal", f"decision chain broken: {msg}", {"chain": "decision"}))
    return findings


def check_reconciliation() -> list[Finding]:
    """Two independent records of the same fact — every `done` order's
    recorded payment, and the SQLite spend ledger — must always sum to the
    same total. A mismatch means money moved that one side doesn't know
    about, in either direction. See reconcile.py for the comparison itself."""
    r = reconcile.reconcile_orders_vs_ledger()
    if not r.matches:
        return [Finding(
            "reconciliation", "fatal",
            f"order files show ₹{r.orders_total_inr:.2f} paid across {r.orders_count} done orders, but "
            f"the ledger records ₹{r.ledger_total_inr:.2f} across {r.ledger_count} entries — these must "
            f"match exactly (discrepancy ₹{r.discrepancy_inr:.2f})",
            {"orders_total_inr": r.orders_total_inr, "orders_count": r.orders_count,
             "ledger_total_inr": r.ledger_total_inr, "ledger_count": r.ledger_count},
        )]
    return []


def check_orphans() -> list[Finding]:
    findings = []
    now = time.time()
    for o in _orders():
        if o.state.value == "approved" and (now - o.updated_at) > STUCK_APPROVED_HOURS * 3600:
            findings.append(Finding(
                "orphans", "warn",
                f"{o.order_id} has sat 'approved' with no execute for over {STUCK_APPROVED_HOURS}h",
                {"order_id": o.order_id, "hours_stuck": round((now - o.updated_at) / 3600, 1)},
            ))
        if o.state.value == "executing" and (now - o.updated_at) > STUCK_EXECUTING_MINUTES * 60:
            findings.append(Finding(
                "orphans", "critical",
                f"{o.order_id} has been 'executing' for over {STUCK_EXECUTING_MINUTES} minutes — "
                f"a job likely died mid-flight and needs a human to look",
                {"order_id": o.order_id, "minutes_stuck": round((now - o.updated_at) / 60, 1)},
            ))
        if o.state.value == "done" and o.payment:
            mandate_path = config.MANDATES_ISSUED / f"payment-{o.payment['payment_id']}.json"
            if not mandate_path.exists():
                findings.append(Finding(
                    "orphans", "fatal",
                    f"{o.order_id} is 'done' with a recorded payment but no matching PaymentMandate "
                    f"on disk — a payment must never exist without the mandate that authorised it",
                    {"order_id": o.order_id, "payment_id": o.payment["payment_id"]},
                ))
    return findings


def check_reasoning_vs_fact() -> list[Finding]:
    """For every order that has both a cart and a recorded comparison,
    confirm the merchant actually carted matches the one the comparison
    itself said was cheapest and eligible — the one case where "the agent
    said X was cheapest but bought Y" would be a real, checkable lie rather
    than a legitimate min-order override (which below_min_order captures)."""
    findings = []
    for o in _orders():
        if not o.cart or not o.comparison:
            continue
        ranked = sorted(o.comparison, key=lambda c: c["rank"])
        if not ranked:
            continue
        winner = ranked[0]
        if (winner["merchant"] != o.cart["merchant"]
                and winner.get("cheapest_by_landed_cost")
                and not winner.get("below_min_order")):
            findings.append(Finding(
                "reasoning_vs_fact", "critical",
                f"{o.order_id}: cart is with {o.cart['merchant']} but the recorded comparison shows "
                f"{winner['merchant']} was cheapest and eligible, with no min-order exception recorded",
                {"order_id": o.order_id, "cart_merchant": o.cart["merchant"], "cheapest_merchant": winner["merchant"]},
            ))
    return findings


def check_behavioral_drift() -> list[Finding]:
    """Compares today's completed orders against the rolling baseline. Needs
    at least 5 historical orders before it will say anything — with less
    history than that, "normal" isn't yet a meaningful claim, and a false
    alarm on day two is how this channel gets ignored."""
    base = baseline.summary()
    if base["n"] < 5:
        return []

    findings = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    todays_done = [
        o for o in _orders()
        if o.state.value == "done" and o.payment
        and datetime.fromtimestamp(o.updated_at, tz=timezone.utc).strftime("%Y-%m-%d") == today
    ]

    if len(todays_done) > max(3, base["mean_orders_per_day"] * 3):
        findings.append(Finding(
            "behavioral_drift", "warn",
            f"{len(todays_done)} orders completed today vs a baseline of {base['mean_orders_per_day']:.1f}/day",
            {"today_count": len(todays_done), "baseline_per_day": base["mean_orders_per_day"]},
        ))

    threshold = base["mean_amount_inr"] + 3 * (base["stdev_amount_inr"] or base["mean_amount_inr"] * 0.5)
    for o in todays_done:
        amount = o.payment["amount_inr"]
        if threshold > 0 and amount > threshold:
            findings.append(Finding(
                "behavioral_drift", "warn",
                f"{o.order_id}: ₹{amount:.2f} is well above the usual order size "
                f"(baseline mean ₹{base['mean_amount_inr']:.2f})",
                {"order_id": o.order_id, "amount_inr": amount, "baseline_mean_inr": base["mean_amount_inr"]},
            ))
        if o.cart and o.cart["merchant"] not in base["merchants_seen"]:
            findings.append(Finding(
                "behavioral_drift", "info",
                f"{o.order_id}: first order ever placed with {o.cart['merchant']}",
                {"order_id": o.order_id, "merchant": o.cart["merchant"]},
            ))
    return findings


def check_adapter_health() -> list[Finding]:
    """Per-merchant miss rate from the decision log: a merchant whose
    lookups are increasingly empty is a silently breaking adapter, catchable
    here well before it shows up to a human as a wrong or missing price."""
    totals: dict[str, dict[str, int]] = defaultdict(lambda: {"ok": 0, "miss": 0})
    for d in decision_log.read_all():
        if d["action"] == "select_item":
            for ev in d.get("evidence", []):
                if ev.get("source"):
                    totals[ev["source"]]["ok"] += 1
        elif d["action"] in ("item_unavailable",):
            for ev in d.get("evidence", []):
                if ev.get("source"):
                    totals[ev["source"]]["miss"] += 1

    findings = []
    for merchant, counts in totals.items():
        total = counts["ok"] + counts["miss"]
        if total >= ADAPTER_MIN_SAMPLE and counts["miss"] / total > ADAPTER_MISS_RATE_THRESHOLD:
            findings.append(Finding(
                "adapter_health", "warn",
                f"{merchant}: {counts['miss']}/{total} recent lookups came back empty — "
                f"the adapter may be broken rather than the catalogue genuinely thin",
                {"merchant": merchant, **counts},
            ))
    return findings


ALL_CHECKS = (
    check_chain_integrity,
    check_reconciliation,
    check_orphans,
    check_reasoning_vs_fact,
    check_behavioral_drift,
    check_adapter_health,
)


def run_all() -> list[Finding]:
    findings: list[Finding] = []
    for fn in ALL_CHECKS:
        findings.extend(fn())
    return findings


def refresh_baseline() -> int:
    """Feed every `done` order into the rolling baseline. Idempotent — call
    this at the start of every sweep. Returns how many were newly recorded."""
    added = 0
    for o in _orders():
        if o.state.value == "done" and o.payment and o.cart:
            before = baseline.summary()["n"]
            baseline.record_order(
                order_id=o.order_id, amount_inr=o.payment["amount_inr"],
                merchant=o.cart["merchant"], ts=o.updated_at,
            )
            after = baseline.summary()["n"]
            added += after - before
    return added
