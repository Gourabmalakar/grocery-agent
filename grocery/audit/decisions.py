"""
The decision log — reasoning, hash-chained the same way as the audit log but
kept as a separate file/chain, because "what happened" and "why" are
different claims with different failure modes.

CLAUDE.md rule 4: every action states its reasoning, its evidence, what it
rejected, and the mandate clause that authorised it. An action with no
DecisionRecord is a defect, not a shortcut.

Important caveat, stated in code as well as in the plan: this is the agent's
own account of itself. Stated reasoning can be post-hoc rationalisation — it
is evidence, not proof. transaction-sentinel cross-checks `evidence` against
independently recomputed facts; it does not just trust `reasoning`.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from grocery import config

_DECISIONS_DIR = config.AUDIT_DIR / "decisions"
_DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
GENESIS_HASH = "0" * 64


def _today_path() -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _DECISIONS_DIR / f"{day}.jsonl"


def _last_entry() -> dict[str, Any] | None:
    for path in sorted(_DECISIONS_DIR.glob("*.jsonl"), reverse=True):
        lines = path.read_text().strip().splitlines()
        if lines:
            return json.loads(lines[-1])
    return None


def record(
    *,
    trace_id: str,
    actor: str,
    action: str,
    reasoning: str,
    evidence: list[dict[str, Any]] | None = None,
    alternatives_rejected: list[dict[str, Any]] | None = None,
    mandate_ref: str | None = None,
    confidence: str = "medium",
    parent_span_id: str | None = None,
) -> dict[str, Any]:
    """Emit one DecisionRecord. `mandate_ref` should point at the intent (or
    amendment) clause that authorised this action, e.g. "intent:abc123#items[2]"
    — the family office's "cite the clause" rule, applied to shopping."""
    span_id = f"span_{uuid.uuid4().hex[:10]}"
    body = {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "ts": time.time(),
        "actor": actor,
        "action": action,
        "reasoning": reasoning,
        "evidence": evidence or [],
        "alternatives_rejected": alternatives_rejected or [],
        "confidence": confidence,
        "mandate_ref": mandate_ref,
    }
    prev = _last_entry()
    prev_hash = prev["entry_hash"] if prev else GENESIS_HASH
    entry_hash = hashlib.sha256(
        prev_hash.encode() + json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    entry = {**body, "prev_hash": prev_hash, "entry_hash": entry_hash}
    with _today_path().open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def trace(trace_id: str) -> list[dict[str, Any]]:
    """All spans for one order, in emission order — what the Console renders
    as the collapsible reasoning tree."""
    out = [e for e in read_all() if e["trace_id"] == trace_id]
    out.sort(key=lambda e: e["ts"])
    return out


def read_all() -> Iterator[dict[str, Any]]:
    for path in sorted(_DECISIONS_DIR.glob("*.jsonl")):
        for line in path.read_text().strip().splitlines():
            if line:
                yield json.loads(line)
