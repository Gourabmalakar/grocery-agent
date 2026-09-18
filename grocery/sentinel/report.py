"""
Wires checks.py's pure findings to actual consequences: an audit-log entry
per finding (so a sweep leaves a trace even when nothing is wrong), PANIC on
a fatal finding, and the "last swept at" timestamp the dead-man's switch in
grocery/killswitch_health.py reads. Kept separate from checks.py so every
check function stays a pure, side-effect-free unit under test — only this
module ever calls killswitch.trip_panic or writes the sweep-timestamp file.

Run standalone as: python -m grocery.sentinel.report
The transaction-sentinel subagent calls sweep() the same way a scheduled
heartbeat would, once Phase 1's scheduler lands (see the plan's heartbeat
section) — until then, run it by hand or from cron.
"""
from __future__ import annotations

import json
import time
from typing import Any

from grocery import config, killswitch
from grocery.audit import log as audit_log
from grocery.sentinel import checks

_LAST_SWEEP_PATH = config.DATA / "sentinel" / "last_sweep.json"
_LAST_SWEEP_PATH.parent.mkdir(parents=True, exist_ok=True)

# A sweep that hasn't reported in this long is itself the anomaly — see the
# plan's dead-man's switch: "silence that looks like a quiet day is the
# failure this system is least allowed to have."
DEAD_MANS_SWITCH_HOURS = 3


def _severity_rank(severity: str) -> int:
    return {"info": 0, "warn": 1, "critical": 2, "fatal": 3}.get(severity, 0)


def sweep(*, trip_on_fatal: bool = True) -> dict[str, Any]:
    """Run every check, log every finding, refresh the baseline, and trip
    PANIC if anything fatal turned up. Returns a JSON-able summary — this is
    what /health surfaces and what a scheduled sentinel run would email."""
    newly_baselined = checks.refresh_baseline()
    findings = checks.run_all()

    for f in findings:
        audit_log.append("sentinel.finding", f.to_json())

    fatal = [f for f in findings if f.severity == "fatal"]
    if fatal and trip_on_fatal and killswitch.state().tier == killswitch.Tier.NONE:
        reasons = "; ".join(f.message for f in fatal)
        killswitch.trip_panic(reason=f"transaction-sentinel: {reasons}", set_by="transaction-sentinel")

    summary = {
        "swept_at": time.time(),
        "findings_count": len(findings),
        "by_severity": {
            sev: len([f for f in findings if f.severity == sev])
            for sev in ("info", "warn", "critical", "fatal")
        },
        "findings": [f.to_json() for f in sorted(findings, key=lambda f: -_severity_rank(f.severity))],
        "newly_baselined_orders": newly_baselined,
        "tripped_panic": bool(fatal and trip_on_fatal),
    }
    _LAST_SWEEP_PATH.write_text(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def last_sweep() -> dict[str, Any] | None:
    if not _LAST_SWEEP_PATH.exists():
        return None
    try:
        return json.loads(_LAST_SWEEP_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def dead_mans_switch_tripped() -> bool:
    """True if the sentinel has never run, or hasn't reported in over
    DEAD_MANS_SWITCH_HOURS — a stale sweep is treated as a finding in its
    own right by /health, same spirit as the kill switch failing closed on
    an unreadable state file."""
    last = last_sweep()
    if last is None:
        return True
    return (time.time() - last["swept_at"]) > DEAD_MANS_SWITCH_HOURS * 3600


def render_text(summary: dict[str, Any]) -> str:
    lines = [f"transaction-sentinel sweep — {summary['findings_count']} finding(s)"]
    for f in summary["findings"]:
        lines.append(f"  [{f['severity'].upper():8s}] {f['check']}: {f['message']}")
    if not summary["findings"]:
        lines.append("  nothing to report — all checks passed")
    return "\n".join(lines)


if __name__ == "__main__":
    result = sweep()
    print(render_text(result))
    raise SystemExit(1 if result["tripped_panic"] else 0)
