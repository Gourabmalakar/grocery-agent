"""
Phase 0-1 auth factor: a passphrase Gourab types, hashed with PBKDF2-HMAC-
SHA256 (matching the family office's ops/vault_crypto.py reasoning — pure
Python, not the openssl CLI, because -pbkdf2's digest default differs
between macOS LibreSSL and Linux OpenSSL and silently produces a different
key from the same passphrase).

This module deliberately does NOT decide what the passphrase gates — that is
the caller's job (approve_payment, clear_panic, etc). It only proves
"the person typing this knows the passphrase", nothing more.

Phase 1 adds WebAuthn passkeys (console/auth/webauthn.py, not yet built) as
a second, biometric factor layered on top for payment approval — see the
plan's authentication section.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

from grocery import config

_HASH_PATH = config.KEYS_DIR / "passphrase.hash"
_ITERATIONS = 300_000


def _derive(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, _ITERATIONS)


def is_set() -> bool:
    return _HASH_PATH.exists()


def set_passphrase(passphrase: str) -> None:
    """First-run setup only. Refuses to silently overwrite an existing
    passphrase — changing it is a deliberate act, not an accidental one."""
    if is_set():
        raise RuntimeError("a passphrase is already set — use rotate_passphrase to change it")
    salt = os.urandom(16)
    digest = _derive(passphrase, salt)
    _HASH_PATH.write_bytes(salt + digest)
    _HASH_PATH.chmod(0o600)


def verify(passphrase: str) -> bool:
    if not is_set():
        return False
    blob = _HASH_PATH.read_bytes()
    salt, stored_digest = blob[:16], blob[16:]
    candidate = _derive(passphrase, salt)
    return hmac.compare_digest(candidate, stored_digest)


def rotate_passphrase(*, old_passphrase: str, new_passphrase: str) -> None:
    if not verify(old_passphrase):
        raise PermissionError("old passphrase did not verify")
    salt = os.urandom(16)
    digest = _derive(new_passphrase, salt)
    _HASH_PATH.write_bytes(salt + digest)
