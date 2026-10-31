"""Nothing committable may contain a credential or a home address.

THIS REPO IS PUBLIC. The day it was published, a `.env.bak.<timestamp>` holding
the live Gemini key, Telegram bot token, Console API token and payment
passphrase was sitting un-ignored in the working tree — created by
tools/set_passphrase.sh, which backed the file up before editing it. The
.gitignore named `.env` exactly, so the backup was one rename away from a
public push. Real test files also carried the owner's Telegram chat id, his
bot's numeric id and his home pincode.

The patterns below describe SHAPES, never values: a test that hardcoded the
real secrets in order to look for them would be the leak it is checking for.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

_FORBIDDEN = {
    "a Google API key":        re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    "a Telegram bot token":    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"),
    "an Indian mobile number": re.compile(r"(?<![\d.])(?:\+?91[\-\s]?)?[6-9]\d{9}(?![\d.])"),
    "a private key block":     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "an AWS access key":       re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "a bearer-looking JWT":    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\."),
    "a slack token":           re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
}

# Files that legitimately describe these shapes rather than containing one.
_EXEMPT = {"tests/adversarial/test_no_secrets_in_the_repo.py"}


def _committable_files() -> list[Path]:
    """Exactly what `git add -A` would stage: tracked plus untracked-not-ignored.
    Checking the working tree instead would miss nothing; checking only tracked
    files would have missed the .env backup entirely."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=_REPO, capture_output=True, text=True, check=True).stdout
    return [_REPO / line for line in out.splitlines() if line.strip()]


def test_no_committable_file_contains_a_credential():
    offenders = []
    for path in _committable_files():
        rel = str(path.relative_to(_REPO))
        if rel in _EXEMPT or not path.is_file():
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for label, pattern in _FORBIDDEN.items():
            match = pattern.search(text)
            if match:
                # Report WHERE and WHAT KIND, never the value itself.
                line = text[:match.start()].count("\n") + 1
                offenders.append(f"{rel}:{line} looks like {label}")
    assert not offenders, "secrets must never be committable:\n  " + "\n  ".join(offenders)


def test_every_env_variant_is_ignored():
    """The exact failure: `.env` was ignored and `.env.bak.1793429230` was not."""
    # -q takes a single pathname, so check them one at a time.
    for name in (".env.bak.123", ".env.local", ".env.save"):
        assert subprocess.run(["git", "check-ignore", "-q", name],
                              cwd=_REPO).returncode == 0, (
            f"{name} is not ignored — a backup or editor save of the real .env "
            f"would be committable")


def test_the_runtime_data_directory_is_ignored():
    """data/ holds the vault, the signing keys, the ledger, the audit chains
    and every order — which carry the delivery address and phone number."""
    for name in ("data/vault/anything.json", "data/keys/anything.pem",
                 "data/orders/order_x.json", "data/ledger.db"):
        assert subprocess.run(["git", "check-ignore", "-q", name],
                              cwd=_REPO).returncode == 0, f"{name} is committable"


def test_no_env_backup_is_sitting_in_the_working_tree():
    """Belt to the ignore rule's braces: even ignored, a file full of live
    secrets next to the source is one `git add -f` or one tarball from being
    shared."""
    strays = [str(p.relative_to(_REPO)) for p in _REPO.glob(".env.*")
              if p.name != ".env.example"]
    assert not strays, f"delete these: {strays}"
