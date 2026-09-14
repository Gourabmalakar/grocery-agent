#!/usr/bin/env bash
# SessionStart — surface budget, pending approvals, recent orders and
# kill-switch state so a cold session doesn't have to ask or re-derive them.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 0

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="/usr/bin/python3"
fi

echo "=== GROCERY AGENT — SESSION CONTEXT ==="

"$PY" - <<'PYEOF' 2>/dev/null
try:
    from grocery import config, killswitch
    from grocery.ledger import store
    from console import approval
    ks = killswitch.state()
    print(f"mode: {config.MODE}   kill switch: {ks.tier.value} {('- ' + ks.reason) if ks.reason else ''}")
    print(f"spend today: ₹{store.spend_today():.0f} / ₹{config.CAP_PER_DAY_INR}   "
          f"this month: ₹{store.spend_this_month():.0f} / ₹{config.CAP_PER_MONTH_INR}")
    orders = approval.list_orders()
    pending = [o for o in orders if o.state.value in ("draft", "proposed", "revising", "approved", "executing")]
    if pending:
        print(f"pending orders ({len(pending)}):")
        for o in pending[-5:]:
            print(f"  - {o.order_id}: {o.state.value}")
    recent_done = [o for o in orders if o.state.value == "done"][-3:]
    if recent_done:
        print("recent completed orders:")
        for o in recent_done:
            print(f"  - {o.order_id}: ₹{o.payment['amount_inr']:.0f} at {o.cart['merchant']}")
except Exception as e:
    print(f"(context unavailable: {e})")
PYEOF

if [ -f "console/PANIC" ]; then
  echo "!!! PANIC is engaged — the broker refuses everything until a human clears it. !!!"
fi
exit 0
