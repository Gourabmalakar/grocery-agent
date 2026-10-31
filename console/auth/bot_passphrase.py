"""The secret that authorises one payment from chat. Verified HERE, never in the bot.

WHY THE CONSOLE OWNS THIS. The bot process is the one exposed to Telegram; it
relays a passphrase and never adjudicates it, so compromising the bot does not
yield the ability to approve — it yields the ability to ask. All payment
authority stays behind console/approval.py, which is bound to 127.0.0.1.

SEPARATE FROM console/auth/passphrase.py ON PURPOSE. That one gates the
Console's own 30-minute live window. This one gates a single payment from a
chat whose transport is not end-to-end encrypted. They are different secrets
guarding different blast radii and they rotate independently; sharing one would
mean a passphrase typed into Telegram also opened the Console.
"""
from __future__ import annotations

import hashlib
import hmac
import os

from grocery import config

_HASH_PATH = config.KEYS_DIR / "bot_passphrase.hash"
_ITERATIONS = 300_000


def _derive(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, _ITERATIONS)


def is_set() -> bool:
    return _HASH_PATH.exists()


def provision_from_config() -> bool:
    """Set the hash from BOT_PASSPHRASE if needed. Idempotent — unlike the
    Console's set_passphrase(), which refuses to overwrite, because this one is
    provisioned at every startup from .env and must be a no-op when unchanged."""
    secret = config.BOT_PASSPHRASE
    if not secret:
        return False
    if is_set() and verify(secret):
        return True
    salt = os.urandom(16)
    _HASH_PATH.parent.mkdir(parents=True, exist_ok=True)
    _HASH_PATH.write_bytes(salt + _derive(secret, salt))
    _HASH_PATH.chmod(0o600)
    return True


def verify(passphrase: str) -> bool:
    """Constant-time. Returns False rather than raising for an unset
    passphrase, so "no passphrase configured" can never read as "approved"."""
    if not is_set() or not passphrase:
        return False
    blob = _HASH_PATH.read_bytes()
    salt, stored = blob[:16], blob[16:]
    return hmac.compare_digest(_derive(passphrase, salt), stored)
