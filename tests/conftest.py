"""
Shared fixtures. Each test gets a fully isolated DATA_DIR (tmp_path) so
mandate keys, the ledger, the audit/decision logs and the kill-switch files
never leak between tests or touch the real repo's data/ directory.
"""
from __future__ import annotations

import importlib
import multiprocessing
import os
import sys
import time
from pathlib import Path

import httpx
import pytest


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret-key-not-for-production")
    monkeypatch.setenv("CAP_PER_ORDER_INR", "800")
    monkeypatch.setenv("CAP_PER_DAY_INR", "1500")
    monkeypatch.setenv("CAP_PER_MONTH_INR", "6000")
    monkeypatch.setenv("MERCHANT_BACKEND", "mock")
    # The test suite must never call a language model: no key, no network, no
    # quota, no latency, and no dependence on what a model said today. This is
    # the same hazard as MERCHANT_BACKEND — grocery/config.py reads .env with
    # load_dotenv(override=False), so a developer's LLM_BACKEND=gemini leaks
    # straight into pytest. It did: the suite went from 18s to 86s of real API
    # calls before this line existed.
    monkeypatch.setenv("LLM_BACKEND", "off")
    monkeypatch.setenv("GEMINI_API_KEY", "")

    # Purge any already-imported grocery/console modules so they re-read the
    # env vars above via a fresh grocery.config import — config.py computes
    # its paths at import time.
    # worker/ is purged alongside grocery/ and console/, and that is not
    # tidiness. worker.llm.client binds `grocery.config` at import; reloading
    # config without dropping the worker leaves the client holding the OLD
    # module object — the one carrying LLM_BACKEND=gemini and the real API key
    # from .env. A test then makes a live, billed call while believing the model
    # is switched off. Caught exactly that way: test_reviser's "no model" case
    # came back with a real interpretation.
    for mod in list(sys.modules):
        root = mod.split(".")[0]
        if root in ("grocery", "console", "worker", "bot"):
            del sys.modules[mod]

    import grocery.config as config
    importlib.reload(config)

    # PANIC_FILE/PAUSE_FILE are deliberately ROOT-relative in production (so
    # `touch console/PANIC` works from any terminal without knowing
    # DATA_DIR) — but that means they are NOT under the isolated data dir
    # above, and would otherwise leak state between tests. Redirect them
    # into tmp_path here so tests stay isolated without changing the
    # production path convention.
    # Set on the module as well as in the env: the env var is read at import,
    # and config has already been reloaded by the time we get here.
    config.KILLSWITCH_DIR = tmp_path
    config.PANIC_FILE = tmp_path / "console_PANIC"
    config.PAUSE_FILE = tmp_path / "console_PAUSE"
    monkeypatch.setenv("KILLSWITCH_DIR", str(tmp_path))
    return config


# ---------------------------------------------------------------- live ----
# Spins up mockstore + console as REAL separate processes on isolated ports,
# pointed at an isolated DATA_DIR — this is the fixture that actually proves
# the worker/console HTTP boundary works, not just that the Python objects
# compose correctly in-process. Originally lived only in tests/e2e/conftest.py;
# moved up to the shared conftest so tests/sentinel/ can drive a real order
# all the way to `done` (through the real mockstore, re-quote included)
# without duplicating this process-spinning logic a second time.
#
# Uses multiprocessing (spawn) rather than an in-process thread precisely so
# each server starts as a genuinely fresh interpreter reading env vars set
# below — the same isolation guarantee isolated_data_dir gives unit tests,
# extended across a real process boundary.

# PANIC/PAUSE are deliberately ROOT-relative even in a subprocess pointed at
# an isolated DATA_DIR — production needs `touch console/PANIC` to work from
# any terminal without knowing DATA_DIR, so grocery.config never puts these
# under it. That's correct in production and a real hazard here: a test that
# trips PANIC through a real subprocess writes the ACTUAL repo file, which
# then poisons every other e2e test in the same run (and, if a test forgets
# to clean up, silently blocks a real console.app run afterward). Belt: any
# test that flips these must clean up itself; braces: this fixture removes
# them unconditionally on teardown regardless of which test left them.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PANIC_FILE = _REPO_ROOT / "console" / "PANIC"
_PAUSE_FILE = _REPO_ROOT / "console" / "PAUSE"


def _run_uvicorn(app_import_string: str, port: int, env: dict):
    os.environ.update(env)
    import uvicorn
    uvicorn.run(app_import_string, host="127.0.0.1", port=port, log_level="warning")


def _wait_for(url: str, timeout: float = 10.0):
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                return
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(0.1)
    raise RuntimeError(f"server at {url} never came up: {last_err}")


@pytest.fixture()
def live(tmp_path):
    mock_port = 18801
    console_port = 18800
    env = {
        "DATA_DIR": str(tmp_path / "data"),
        "SESSION_SECRET_KEY": "test-secret-key-not-for-production",
        "MOCKSTORE_URL": f"http://127.0.0.1:{mock_port}",
        "CONSOLE_URL": f"http://127.0.0.1:{console_port}",
        "CAP_PER_ORDER_INR": "800",
        "CAP_PER_DAY_INR": "1500",
        "CAP_PER_MONTH_INR": "6000",
        "MERCHANT_BACKEND": "mock",
        "LLM_BACKEND": "off",
        "GEMINI_API_KEY": "",
        # So a subprocess that trips PANIC writes into tmp, not into the repo —
        # where it would stop a worker the human is running right now.
        "KILLSWITCH_DIR": str(tmp_path / "killswitch"),
        # The Console subprocess needs this to recognise the bot; monkeypatching
        # it in the test process cannot reach a process already spawned.
        "BOT_API_TOKEN": "test-bot-token",
    }

    (tmp_path / "killswitch").mkdir(parents=True, exist_ok=True)
    ctx = multiprocessing.get_context("spawn")
    mock_proc = ctx.Process(target=_run_uvicorn, args=("mockstore.app:app", mock_port, env))
    console_proc = ctx.Process(target=_run_uvicorn, args=("console.app:app", console_port, env))
    mock_proc.start()
    console_proc.start()
    try:
        _wait_for(f"http://127.0.0.1:{mock_port}/health")
        _wait_for(f"http://127.0.0.1:{console_port}/health")
        for k, v in env.items():
            os.environ[k] = v  # so the test's own client-side code (worker.tools, adapters) agrees

        # grocery.config (and anything that read a URL constant off it at
        # import time, e.g. worker.tools's CONSOLE_URL) may already be
        # cached in sys.modules from an earlier test in this pytest process,
        # bound to a stale default. Purge so the test's own imports below
        # bind fresh against the env just set, exactly as a real separate
        # `python -m worker.run` process would.
        for mod in list(sys.modules):
            if mod.split(".")[0] in ("grocery", "console", "worker", "mockstore", "bot"):
                del sys.modules[mod]

        yield {"mock": f"http://127.0.0.1:{mock_port}", "console": f"http://127.0.0.1:{console_port}"}
    finally:
        mock_proc.terminate()
        console_proc.terminate()
        mock_proc.join(timeout=5)
        console_proc.join(timeout=5)
        _PANIC_FILE.unlink(missing_ok=True)
        _PAUSE_FILE.unlink(missing_ok=True)
