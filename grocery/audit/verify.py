"""
Recompute both hash chains from scratch and report the first break, if any.

This is what makes the audit trail evidence rather than narrative: anyone —
not just this codebase — can recompute entry_hash from prev_hash + the entry
body and confirm nothing was altered or removed. No model required.

Run as: python -m grocery.audit.verify
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from grocery.audit import log as audit_log
from grocery.audit import decisions as decision_log


class ChainBroken(Exception):
    def __init__(self, chain: str, seq_or_span: Any, reason: str):
        super().__init__(f"{chain} chain broken at {seq_or_span}: {reason}")
        self.chain = chain


def verify_audit_chain() -> tuple[bool, str]:
    prev_hash = audit_log.GENESIS_HASH
    for entry in audit_log.read_all():
        expected = hashlib.sha256(
            prev_hash.encode()
            + audit_log._canonical(entry["seq"], entry["ts"], entry["event"], entry["detail"])
        ).hexdigest()
        if entry["prev_hash"] != prev_hash:
            return False, f"seq {entry['seq']}: prev_hash does not match predecessor"
        if entry["entry_hash"] != expected:
            return False, f"seq {entry['seq']}: entry_hash does not match recomputed value — tampered"
        prev_hash = entry["entry_hash"]
    return True, "ok"


def verify_decision_chain() -> tuple[bool, str]:
    prev_hash = decision_log.GENESIS_HASH
    for entry in decision_log.read_all():
        body = {k: v for k, v in entry.items() if k not in ("prev_hash", "entry_hash")}
        expected = hashlib.sha256(
            prev_hash.encode() + json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if entry["prev_hash"] != prev_hash:
            return False, f"span {entry['span_id']}: prev_hash does not match predecessor"
        if entry["entry_hash"] != expected:
            return False, f"span {entry['span_id']}: entry_hash does not match recomputed value — tampered"
        prev_hash = entry["entry_hash"]
    return True, "ok"


if __name__ == "__main__":
    a_ok, a_msg = verify_audit_chain()
    d_ok, d_msg = verify_decision_chain()
    print(f"audit chain:    {'OK' if a_ok else 'BROKEN'} — {a_msg}")
    print(f"decision chain: {'OK' if d_ok else 'BROKEN'} — {d_msg}")
    raise SystemExit(0 if (a_ok and d_ok) else 1)
