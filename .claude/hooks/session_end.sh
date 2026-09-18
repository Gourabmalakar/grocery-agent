#!/usr/bin/env bash
# SessionEnd / PreCompact — the human-written half of session memory.
# compact_session.py already wrote the mechanical digest; this is the
# reminder to also update memory/LATEST.md by hand with anything that digest
# can't capture (decisions, rationale, surprises).
set -uo pipefail
cat <<'MSG'
[session-memory] If this session established a decision, a surprising fact,
or changed how the system should behave, update memory/LATEST.md by hand —
it is the one file rewritten (never appended) that the next session reads
first. The mechanical digest in memory/conversations/ records what happened;
it does not record why.

[docs-sync] If this session added or changed a module, route, check class,
or screen, confirm README.md's "What's built" list and CLAUDE.md's
architecture section still match reality before ending — a stale design doc
is worse than an absent one because the next session trusts it. This is not
optional busywork; skipping it is how "the plan says X" and "the code does Y"
quietly drift apart.
MSG
exit 0
