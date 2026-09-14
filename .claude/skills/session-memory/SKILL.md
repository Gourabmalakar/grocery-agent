---
name: session-memory
description: Compact each working session into a timestamped summary so future sessions get context without re-reading the repo. Use at the end of every session, when context is running long, and at the start of a session to load prior state cheaply.
---

# Session Memory

Ported from the family office's skill of the same name — same reasoning, new
subject matter.

## At the start of a session

1. `CLAUDE.md` — always.
2. `memory/LATEST.md` — the rolling state summary, always.
3. The two or three most recent `memory/conversations/*.md`.
4. Only then, whatever specific files the task actually needs.

**Do not read the audit or decision logs in full to answer a general
question.** Query them (`grocery.audit.log.read_recent`,
`grocery.audit.decisions.trace`) for the slice you need.

## At the end of a session

The `compact_session.py` hook already writes a mechanical digest to
`memory/conversations/` on `SessionEnd`/`PreCompact` — it costs zero tokens
and runs whether or not the model cooperates. Your job is the part it
cannot do: if this session established a decision, a surprising fact, or
changed how the system should behave, write it into `memory/LATEST.md` by
hand (rewritten, never appended) — that is the file the next session
actually reads first.

## Rules

- Keep decisions and facts. Drop the deliberation that led to them.
- Never store a real credential, token, or wallet balance in a memory file.
- A number gets a date. "Spend this month ₹1,240" is only true on the day
  it was written.
