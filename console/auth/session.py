"""
Minimal signed-cookie session so the Console distinguishes "a browser tab
that has typed the passphrase this run" from an anonymous request. Not a
multi-user auth system — this console has exactly one user by design.
"""
from __future__ import annotations

import time

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from grocery import config

_serializer = URLSafeTimedSerializer(config.SESSION_SECRET_KEY, salt="grocery-console-session")
_SESSION_MAX_AGE_SECONDS = 60 * 60 * 8  # 8h — a long day of shopping, not indefinite


def make_session_token(*, unlocked_at: float | None = None) -> str:
    return _serializer.dumps({"unlocked_at": unlocked_at or time.time()})


def read_session_token(token: str) -> dict | None:
    try:
        return _serializer.loads(token, max_age=_SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def is_within_live_window(token: str, *, window_seconds: int = 1800) -> bool:
    """The 30-minute 'live window' after typing the passphrase, per the plan
    — the decrypted vault key (Phase 4) is zeroed once this expires, and
    payment approval above ₹1,000 re-checks this rather than trusting an
    old session indefinitely."""
    data = read_session_token(token)
    if not data:
        return False
    return (time.time() - data["unlocked_at"]) <= window_seconds
