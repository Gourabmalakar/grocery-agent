#!/usr/bin/env bash
# PostToolUse(browser tools) — append every merchant-domain action to the
# audit log, so browser-adapter activity (Phase 2+) is visible in the same
# trail as everything else, not a silent gap alongside it.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 0
input=$(cat)

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="/usr/bin/python3"
fi

printf '%s' "$input" | "$PY" - <<'PYEOF' 2>/dev/null
import json, sys
try:
    data = json.load(sys.stdin)
    ti = data.get("tool_input", {})
    url = ti.get("url", "")
    if url:
        from grocery.audit import log as audit_log
        audit_log.append("browser.navigate", {"url": url, "tool": data.get("tool_name", "")})
except Exception:
    pass
PYEOF
exit 0
