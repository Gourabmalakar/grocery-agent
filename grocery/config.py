"""
Paths, caps and mode — the one place every other module reads these from.

Mirrors the family office's engine/config.py pattern: derive ROOT once, create
data directories eagerly, read env through a single tolerant accessor, and
treat everything here as data an agent might reason about rather than a
constant buried in code.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # Scripts and cron-style invocations behave identically without every
    # caller having to remember to source .env first.
    pass


def _env(name: str, default: str = "") -> str:
    """Read an env var, tolerating shell-style quoting in .env values."""
    return os.getenv(name, default).strip().strip('"').strip("'")


ROOT = Path(__file__).resolve().parent.parent
DATA = Path(_env("DATA_DIR", str(ROOT / "data"))).resolve()
KEYS_DIR = DATA / "keys"
VAULT_DIR = DATA / "vault"
AUDIT_DIR = DATA / "audit"
LEDGER_DB = DATA / "ledger.db"
MANDATES_ISSUED = DATA / "mandates" / "issued"

for _d in (DATA, KEYS_DIR, VAULT_DIR, AUDIT_DIR, MANDATES_ISSUED):
    _d.mkdir(parents=True, exist_ok=True)

# MODE is the single most safety-critical value in the whole project.
# Anything other than the literal string "live" behaves as dry_run: the
# ledger still enforces caps and the mandates still get signed, but the
# rails write to the mock rail instead of touching a real merchant/wallet.
MODE = _env("MODE", "dry_run")
IS_LIVE = MODE == "live"

MOCKSTORE_URL = _env("MOCKSTORE_URL", "http://127.0.0.1:8801")
CONSOLE_URL = _env("CONSOLE_URL", "http://127.0.0.1:8800")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")

# Hard caps, in INR. Enforced in grocery/ledger/budget.py against the SQLite
# ledger before every execute — never in a prompt, never by the model.
CAP_PER_ORDER_INR = int(_env("CAP_PER_ORDER_INR", "800") or "800")
CAP_PER_DAY_INR = int(_env("CAP_PER_DAY_INR", "1500") or "1500")
CAP_PER_MONTH_INR = int(_env("CAP_PER_MONTH_INR", "6000") or "6000")

# Bounded revision rounds (see console/revision.py) — a loop between a
# confused agent and a frustrated human must terminate.
MAX_REVISION_ROUNDS = 5

SESSION_SECRET_KEY = _env("SESSION_SECRET_KEY", "") or "dev-only-insecure-key-set-one-in-.env"

# Kill-switch state files. Plain files, deliberately: containment must work
# from a terminal even if the console process is dead.
PANIC_FILE = ROOT / "console" / "PANIC"
PAUSE_FILE = ROOT / "console" / "PAUSE"
