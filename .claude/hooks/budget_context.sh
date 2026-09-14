#!/usr/bin/env bash
# UserPromptSubmit — inject remaining budget, mode, and kill-switch state
# into every turn, so a session reasons against current numbers rather than
# stale ones it might remember from earlier in a long conversation.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 0

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="/usr/bin/python3"
fi

"$PY" - <<'PYEOF' 2>/dev/null
try:
    from grocery import config, killswitch
    from grocery.ledger import store
    ks = killswitch.state()
    print(f"[grocery-agent] mode={config.MODE} kill_switch={ks.tier.value} "
          f"spend_today=₹{store.spend_today():.0f}/₹{config.CAP_PER_DAY_INR} "
          f"spend_month=₹{store.spend_this_month():.0f}/₹{config.CAP_PER_MONTH_INR}")
except Exception:
    pass
PYEOF
exit 0
