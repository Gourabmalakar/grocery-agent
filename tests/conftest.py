"""
Shared fixtures. Each test gets a fully isolated DATA_DIR (tmp_path) so
mandate keys, the ledger, the audit/decision logs and the kill-switch files
never leak between tests or touch the real repo's data/ directory.
"""
from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret-key-not-for-production")
    monkeypatch.setenv("CAP_PER_ORDER_INR", "800")
    monkeypatch.setenv("CAP_PER_DAY_INR", "1500")
    monkeypatch.setenv("CAP_PER_MONTH_INR", "6000")

    # Purge any already-imported grocery/console modules so they re-read the
    # env vars above via a fresh grocery.config import — config.py computes
    # its paths at import time.
    for mod in list(sys.modules):
        if mod == "grocery" or mod.startswith("grocery.") or mod == "console" or mod.startswith("console."):
            del sys.modules[mod]

    import grocery.config as config
    importlib.reload(config)

    # PANIC_FILE/PAUSE_FILE are deliberately ROOT-relative in production (so
    # `touch console/PANIC` works from any terminal without knowing
    # DATA_DIR) — but that means they are NOT under the isolated data dir
    # above, and would otherwise leak state between tests. Redirect them
    # into tmp_path here so tests stay isolated without changing the
    # production path convention.
    config.PANIC_FILE = tmp_path / "console_PANIC"
    config.PAUSE_FILE = tmp_path / "console_PAUSE"
    return config
