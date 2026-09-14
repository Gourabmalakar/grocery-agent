#!/usr/bin/env python3
"""Archive and compact a session transcript — deterministically, no model needed.

Runs on SessionEnd and PreCompact. Claude Code hands the hook a JSON object on
stdin containing `transcript_path`, so this reads the real conversation off
disk and distils it with plain code.

That matters: a hook is a shell command and cannot ask Claude for a summary.
An instruction-only hook ("please write a compaction") depends on the model
still having budget and attention at the end of a session — exactly when it has
neither. This runs whether or not Claude cooperates, and costs zero tokens.

What the next session reads is the digest, not the transcript: the user's own
requests, decisions, files touched and commands run. That is what makes a cold
start cheap.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "memory" / "conversations"
ARCHIVE = ROOT / "memory" / "transcripts"
MAX_USER_MSGS = 40


def read_event() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def text_of(content) -> str:
    """Message content is either a string or a list of typed blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n".join(parts)
    return ""


def parse(transcript: Path) -> dict:
    """Pull the durable facts out of a JSONL transcript."""
    users, tools, files, commands = [], [], set(), []
    if not transcript.exists():
        return {"users": [], "tools": [], "files": [], "commands": []}

    with open(transcript, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue

            msg = entry.get("message") or {}
            role = msg.get("role") or entry.get("type")

            if role == "user":
                body = text_of(msg.get("content"))
                # Skip tool-result echoes and system-injected reminders; only a
                # human's own words belong in the digest.
                if body and "<system-reminder>" not in body and not body.startswith("["):
                    users.append(" ".join(body.split())[:400])

            for block in (msg.get("content") or []) if isinstance(msg.get("content"), list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                tools.append(name)
                inp = block.get("input") or {}
                if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                    fp = inp.get("file_path")
                    if fp:
                        files.add(str(fp).replace(str(ROOT) + "/", ""))
                elif name == "Bash":
                    cmd = " ".join(str(inp.get("command", "")).split())
                    if cmd:
                        commands.append(cmd[:180])

    return {"users": users, "tools": tools, "files": sorted(files), "commands": commands}


def main() -> int:
    event = read_event()
    transcript = Path(event.get("transcript_path") or "")
    reason = event.get("hook_event_name") or event.get("reason") or "session_end"
    session_id = (event.get("session_id") or "unknown")[:8]

    OUT.mkdir(parents=True, exist_ok=True)
    ARCHIVE.mkdir(parents=True, exist_ok=True)

    facts = parse(transcript)
    if not facts["users"] and not facts["files"]:
        # Nothing happened worth recording. Writing an empty file every time
        # would bury the sessions that mattered.
        return 0

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    path = OUT / ("%s-%s-auto.md" % (stamp, session_id))

    # Keep a raw copy: the digest is lossy and occasionally the detail matters.
    if transcript.exists():
        try:
            (ARCHIVE / ("%s-%s.jsonl" % (stamp, session_id))).write_bytes(
                transcript.read_bytes())
        except OSError:
            pass

    users = facts["users"][:MAX_USER_MSGS]
    counts = {}
    for t in facts["tools"]:
        counts[t] = counts.get(t, 0) + 1
    tool_line = ", ".join("%s x%d" % (k, v)
                          for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:8])

    lines = [
        "---",
        "date: %s" % datetime.now().isoformat(timespec="seconds"),
        "session: %s" % session_id,
        "trigger: %s" % reason,
        "generated: automatically by the compact_session hook",
        "---",
        "",
        "# Session %s" % stamp,
        "",
        "## What was asked for",
    ]
    lines += ["%d. %s" % (i + 1, u) for i, u in enumerate(users)] or ["(nothing recorded)"]

    if facts["files"]:
        lines += ["", "## Files created or changed (%d)" % len(facts["files"])]
        lines += ["- `%s`" % f for f in facts["files"][:60]]
        if len(facts["files"]) > 60:
            lines.append("- ...and %d more" % (len(facts["files"]) - 60))

    if facts["commands"]:
        lines += ["", "## Notable commands"]
        seen, keep = set(), []
        for c in facts["commands"]:
            key = c.split()[0] if c.split() else c
            if key in seen:
                continue
            seen.add(key)
            keep.append(c)
        lines += ["- `%s`" % c for c in keep[:20]]

    if tool_line:
        lines += ["", "## Tool usage", tool_line]

    lines += [
        "",
        "## Note",
        "This digest was generated mechanically from the transcript. It records",
        "*what happened*, not the reasoning. If this session established a fact",
        "worth keeping — something surprising, a correction, a decision and its",
        "rationale — write it into `memory/LATEST.md` by hand, which is the file",
        "the next session actually reads first.",
        "",
        "Full transcript archived at `memory/transcripts/%s-%s.jsonl`." % (stamp, session_id),
    ]

    path.write_text("\n".join(lines), encoding="utf-8")
    print("[compact] wrote %s (%d user messages, %d files touched)"
          % (path.relative_to(ROOT), len(users), len(facts["files"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
