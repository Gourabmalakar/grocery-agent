"""The default run must be network-free without anyone remembering to make it so.

grocery/config.py calls load_dotenv(override=False) at import, so a `.env`
carrying MERCHANT_BACKEND=real leaks into the test suite — and the failure is not
a clean error. It launches Chromium mid-suite, hits three live merchants, and
makes every selection test depend on today's quick-commerce prices.

Both fixtures pin "mock". This file exists so that the next person who edits
`.env` — or deletes a setenv line thinking it redundant — finds out from a red
test rather than from a browser window opening during `pytest`.
"""
from __future__ import annotations

import os

import httpx


def test_isolated_data_dir_forces_the_mock_backend(isolated_data_dir):
    assert isolated_data_dir.MERCHANT_BACKEND == "mock"


def test_no_test_may_call_a_language_model(isolated_data_dir):
    """Same hazard as the merchant backend, discovered the same way: a developer
    .env carrying LLM_BACKEND=gemini leaked into pytest and turned an 18-second
    offline suite into 86 seconds of real Gemini calls — billed to a free tier,
    and making every selection test depend on what a model said today."""
    from worker.llm import client

    assert isolated_data_dir.LLM_BACKEND == "off"
    assert isolated_data_dir.GEMINI_API_KEY == ""
    usable, why = client.available()
    assert not usable and "switched off" in why


def test_the_live_fixture_also_has_no_model(live):
    import os

    assert os.environ["LLM_BACKEND"] == "off"
    assert os.environ["GEMINI_API_KEY"] == ""


def test_live_fixture_forces_the_mock_backend(live):
    """Checked in the fixture's own environment, which is what the worker-side
    code in an e2e test actually reads."""
    assert os.environ["MERCHANT_BACKEND"] == "mock"

    from grocery import config
    assert config.MERCHANT_BACKEND == "mock"


def test_mock_backend_returns_only_mock_adapters(isolated_data_dir):
    """Belt to the braces: even if the env said something else, this asserts
    what "mock" resolves to, so a real adapter can never appear in a default
    run without this failing."""
    from worker import backends

    merchants = {a.merchant_id for a in backends.adapters_for_backend("mock")}
    assert merchants == {"freshmart", "quickbasket", "dailygrocer"}
    assert not merchants & {"jiomart", "blinkit", "zepto", "swiggy"}


def test_console_health_is_reachable_without_touching_a_real_merchant(live):
    """A smoke check that the e2e stack comes up on mock alone — no token, no
    browser, no network beyond localhost."""
    r = httpx.get(f"{live['console']}/health", timeout=10)
    assert r.status_code == 200
