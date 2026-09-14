"""
The kill switch — three tiers, checked at the top of every broker request.

PAUSE  — no new jobs, no execute; in-flight read-only work finishes.
PANIC  — the broker refuses everything: propose, approve, execute.
BURN   — deletes the encrypted credential blob (see console/rails/wallet_rail.py)
         and prints the manual revocation checklist. Irreversible.

Fails closed by construction: state() catches any read error and returns
PANIC rather than propagating an exception a caller might swallow. A kill
switch that fails open on a filesystem hiccup is not a kill switch.

console/PANIC and console/PAUSE are plain files, not database rows,
deliberately — `touch console/PANIC` from any terminal must work even if the
console process and its database are both dead. guard_immutable.sh blocks the
agent from deleting or editing these files from inside a Claude session.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from grocery import config
from grocery.audit import log as audit_log


class Tier(str, Enum):
    NONE = "none"
    PAUSE = "pause"
    PANIC = "panic"


@dataclass
class KillState:
    tier: Tier
    reason: str = ""


def state() -> KillState:
    try:
        if config.PANIC_FILE.exists():
            reason = config.PANIC_FILE.read_text().strip() or "(no reason recorded)"
            return KillState(Tier.PANIC, reason)
        if config.PAUSE_FILE.exists():
            reason = config.PAUSE_FILE.read_text().strip() or "(no reason recorded)"
            return KillState(Tier.PAUSE, reason)
        return KillState(Tier.NONE)
    except OSError:
        # Cannot even read the state files — treat as the most restrictive
        # tier rather than assume "none". Fails closed.
        return KillState(Tier.PANIC, "kill-switch state unreadable — failing closed")


def trip_panic(*, reason: str, set_by: str) -> None:
    config.PANIC_FILE.write_text(f"[{set_by} @ {time.time()}] {reason}\n")
    audit_log.append("killswitch.panic", {"reason": reason, "set_by": set_by})


def trip_pause(*, reason: str, set_by: str) -> None:
    config.PAUSE_FILE.write_text(f"[{set_by} @ {time.time()}] {reason}\n")
    audit_log.append("killswitch.pause", {"reason": reason, "set_by": set_by})


def clear_panic(*, cleared_by: str, typed_reason: str) -> None:
    """Clearing PANIC is a human act, gated by the console's own
    passkey+passphrase check before this is ever called — this function does
    not itself check credentials, so it must never be exposed to the worker's
    tool surface."""
    config.PANIC_FILE.unlink(missing_ok=True)
    audit_log.append("killswitch.panic_cleared", {"cleared_by": cleared_by, "reason": typed_reason})


def clear_pause(*, cleared_by: str) -> None:
    config.PAUSE_FILE.unlink(missing_ok=True)
    audit_log.append("killswitch.pause_cleared", {"cleared_by": cleared_by})


def require_not_killed(action: str) -> None:
    """Call at the top of every broker-side mutating operation. Raises if
    anything is tripped; PAUSE blocks new work same as PANIC does here — the
    distinction between the tiers is about what a human sees and how it's
    cleared, not about which operations they gate."""
    s = state()
    if s.tier != Tier.NONE:
        raise KillSwitchEngaged(f"{action} refused — kill switch is {s.tier.value}: {s.reason}")


class KillSwitchEngaged(Exception):
    pass


BURN_CHECKLIST = """
BURN executed. The encrypted credential vault has been deleted. This does NOT
by itself revoke anything server-side — do these now, in order:

  1. Blinkit / Zepto / BigBasket (whichever wallet rail was live):
     log in on your phone and tap "Log out of all devices" / "Log out
     everywhere" in account settings.
  2. Swiggy (if the Builders Club OAuth token was in use):
     revoke the grant at https://www.swiggy.com/account/apps (or equivalent
     connected-apps page) — this invalidates the token immediately, not just
     locally.
  3. The prepaid/merchant wallet balance itself: move any remaining balance
     out if the instrument supports it, or note it as a bounded, accepted loss.
  4. Re-onboarding after this requires re-registering a passkey and re-running
     the Phase-4 live-mode checklist from scratch. That is intentional.
"""


def burn(*, executed_by: str) -> str:
    from grocery import config as _config
    vault_files = list(_config.VAULT_DIR.glob("*"))
    for f in vault_files:
        f.unlink(missing_ok=True)
    trip_panic(reason="BURN executed — credentials destroyed", set_by=executed_by)
    audit_log.append("killswitch.burn", {"executed_by": executed_by, "files_deleted": len(vault_files)})
    return BURN_CHECKLIST
