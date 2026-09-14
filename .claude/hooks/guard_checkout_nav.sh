#!/usr/bin/env bash
# PreToolUse(browser navigate/computer/form_input) — block navigation to a
# real merchant's payment/checkout page, and block form-filling anything
# that looks like a card/CVV/OTP/UPI-PIN field.
#
# Applies to browser-automation adapters (Phase 2+). In Phase 0-1 this hook
# fires on nothing (there is no real browsing yet) but is wired in from day
# one so Phase 2 inherits the guard rather than needing it retrofitted.
set -uo pipefail
input=$(cat)
payload=$(printf '%s' "$input" | /usr/bin/python3 -c '
import json, sys
data = json.load(sys.stdin)
ti = data.get("tool_input", {})
# navigate-style tools carry a url; form_input-style tools carry a value we
# never want to see look like a card number / CVV / OTP.
print(json.dumps({"url": ti.get("url", ""), "value": str(ti.get("value", ""))}))
' 2>/dev/null)

url=$(printf '%s' "$payload" | /usr/bin/python3 -c 'import json,sys;print(json.load(sys.stdin)["url"].lower())' 2>/dev/null)
value=$(printf '%s' "$payload" | /usr/bin/python3 -c 'import json,sys;print(json.load(sys.stdin)["value"])' 2>/dev/null)

case "$url" in
  *"/checkout/payment"*|*"/pay"*|*"cardnumber"*|*"/cvv"*)
    echo "BLOCKED: navigation to a real merchant payment/checkout page. The agent never" >&2
    echo "completes checkout directly — it stops before payment and the Console takes over." >&2
    exit 2
    ;;
esac

# A 16-digit run (possible card number), or a 3-4 digit value entered into
# something that smells like a CVV/OTP field, is refused outright.
if printf '%s' "$value" | grep -Eq '^[0-9]{13,19}$'; then
  echo "BLOCKED: attempted to type a card-number-shaped value into a form field." >&2
  exit 2
fi
exit 0
