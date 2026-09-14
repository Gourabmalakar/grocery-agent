#!/usr/bin/env bash
# PreToolUse(Edit|Write|MultiEdit) — refuse hand-edits to append-only or
# safety-critical paths. Exit 2 blocks the tool call and returns stderr to
# the model, same contract as the family office's guard_generated_files.sh.
set -uo pipefail
input=$(cat)
path=$(printf '%s' "$input" | /usr/bin/python3 -c \
  'import json,sys;print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' 2>/dev/null)

case "$path" in
  */data/audit/*)
    echo "BLOCKED: data/audit/ is the hash-chained append-only log. Editing it breaks the" >&2
    echo "chain and destroys the one thing that makes this system's history verifiable." >&2
    exit 2
    ;;
  */data/ledger.db)
    echo "BLOCKED: data/ledger.db is the spend ledger the budget caps are checked against." >&2
    echo "It is written only by grocery/ledger/store.py's record_spend(), never by hand." >&2
    exit 2
    ;;
  */data/mandates/issued/*)
    echo "BLOCKED: data/mandates/issued/ is append-only — a mandate, once signed, is never" >&2
    echo "edited. A revision issues a new signed mandate/amendment instead." >&2
    exit 2
    ;;
  */.env|*/.env.*)
    echo "BLOCKED: refusing to let an agent session edit env files that may hold secrets." >&2
    echo "Edit .env yourself, outside a Claude session." >&2
    exit 2
    ;;
  */console/PANIC|*/console/PAUSE)
    echo "BLOCKED: the agent must not be able to clear or alter the kill-switch state files" >&2
    echo "itself. Clearing PANIC/PAUSE requires the Console's own passphrase-gated route." >&2
    exit 2
    ;;
  */console/app.py|*/console/approval.py|*/console/rails/*|*/console/auth/*|*/grocery/killswitch.py)
    echo "BLOCKED: this file is part of the broker/security boundary. The agent must not be" >&2
    echo "able to edit the code that constrains it from inside a working session." >&2
    echo "If this genuinely needs to change, do it in a review outside an autonomous session." >&2
    exit 2
    ;;
esac
exit 0
