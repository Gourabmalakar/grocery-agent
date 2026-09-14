#!/usr/bin/env bash
# PreToolUse(Bash) — refuse any shell command that reaches a payment endpoint
# or a merchant checkout/order path directly, bypassing the broker's
# approval state machine entirely.
#
# The console's /console/* routes are the only legal path to money moving.
# A bash `curl` straight at a payment rail, or at mockstore's /order route,
# is exactly the bypass the two-process architecture exists to make
# unreachable — this hook is the belt on top of that structural braces.
set -uo pipefail
input=$(cat)
cmd=$(printf '%s' "$input" | /usr/bin/python3 -c \
  'import json,sys;print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null)

lc=$(printf '%s' "$cmd" | tr '[:upper:]' '[:lower:]')

case "$lc" in
  *"/order"*|*"/api/cron"*|*"place_order"*|*"wallet_rail"*|*"/console/order/"*"/approve_payment"*)
    echo "BLOCKED: this command appears to call a payment/order endpoint directly from a shell." >&2
    echo "Payment can only happen through the Console's own approval flow (a human clicking" >&2
    echo "Approve payment), never via a bash command reaching the endpoint directly." >&2
    exit 2
    ;;
esac
exit 0
