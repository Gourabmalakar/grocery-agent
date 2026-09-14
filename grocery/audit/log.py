"""
The audit log — facts, hash-chained, append-only.

Records state transitions, money, mandate references and hashes. Verifiable
by recomputation without a model in the loop (see verify.py), which is what
makes it evidence rather than a narrative. Never edited — see
.claude/hooks/guard_immutable.sh, which blocks writes to this file from
inside a Claude session.

One JSONL file per UTC day under config.AUDIT_DIR, each line:
  {seq, ts, prev_hash, entry_hash, event, detail}
where entry_hash = sha256(prev_hash + canonical(event, detail, seq, ts)).
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from grocery import config

GENESIS_HASH = "0" * 64


def _today_path() -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return config.AUDIT_DIR / f"{day}.jsonl"


def _canonical(seq: int, ts: float, event: str, detail: dict[str, Any]) -> bytes:
    return json.dumps(
        {"seq": seq, "ts": ts, "event": event, "detail": detail},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _last_entry() -> dict[str, Any] | None:
    """Walk backwards from today across files until we find the most recent
    entry, so the chain is continuous across day boundaries."""
    for path in sorted(config.AUDIT_DIR.glob("*.jsonl"), reverse=True):
        lines = path.read_text().strip().splitlines()
        if lines:
            return json.loads(lines[-1])
    return None


def append(event: str, detail: dict[str, Any]) -> dict[str, Any]:
    """Append one fact. Returns the written entry. Never raises to avoid
    silently dropping an audit record if the entry itself is malformed —
    always writes what it can, defaulting unhashable detail to its repr."""
    try:
        json.dumps(detail)
    except TypeError:
        detail = {"_unserialisable_repr": repr(detail)}

    prev = _last_entry()
    seq = (prev["seq"] + 1) if prev else 0
    prev_hash = prev["entry_hash"] if prev else GENESIS_HASH
    ts = time.time()
    entry_hash = hashlib.sha256(prev_hash.encode() + _canonical(seq, ts, event, detail)).hexdigest()

    entry = {
        "seq": seq, "ts": ts, "event": event, "detail": detail,
        "prev_hash": prev_hash, "entry_hash": entry_hash,
    }
    path = _today_path()
    with path.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def read_all() -> Iterator[dict[str, Any]]:
    for path in sorted(config.AUDIT_DIR.glob("*.jsonl")):
        for line in path.read_text().strip().splitlines():
            if line:
                yield json.loads(line)


def read_recent(n: int = 20) -> list[dict[str, Any]]:
    entries = list(read_all())
    return entries[-n:]
