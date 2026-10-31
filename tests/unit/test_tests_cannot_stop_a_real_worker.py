"""The test suite must not be able to halt a real order.

PANIC and PAUSE are deliberately at a fixed repo-relative path so a human can
`touch console/PANIC` from any terminal without knowing where DATA_DIR points.
That is right, and it had a consequence nobody had thought through: the e2e
fixtures spawn REAL subprocesses, so a test that trips PANIC wrote the ACTUAL
repo file — and any worker the human was running at that moment stopped dead.

Observed exactly once, and it cost an afternoon of looking in the wrong place:

    20:01:38 [worker] re-planning after revision: 'add mmul butter 250g'
    20:05:28 [worker] kill switch is panic — stopping immediately

The order looked stuck for reasons that had nothing to do with the order.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def test_the_kill_switch_is_redirected_away_from_the_repo(isolated_data_dir):
    """A test that trips PANIC must write somewhere harmless."""
    panic = isolated_data_dir.PANIC_FILE
    assert _REPO not in panic.parents or "console" not in str(panic), (
        f"tests would write the real kill switch at {panic}"
    )
    assert isolated_data_dir.KILLSWITCH_DIR != _REPO / "console"


def test_tripping_panic_in_a_test_does_not_touch_the_repo_file(isolated_data_dir):
    from grocery import killswitch

    real_panic = _REPO / "console" / "PANIC"
    existed_before = real_panic.exists()

    isolated_data_dir.PANIC_FILE.parent.mkdir(parents=True, exist_ok=True)
    isolated_data_dir.PANIC_FILE.write_text("tripped by a test")
    assert killswitch.state().tier.value == "panic"

    assert real_panic.exists() == existed_before, (
        "a test tripped the kill switch and it reached the real repo path"
    )


def test_production_still_uses_the_documented_path(isolated_data_dir, monkeypatch):
    """The default must stay `console/PANIC` — the whole point is that a human
    in a hurry can touch it without reading any configuration."""
    import importlib

    monkeypatch.delenv("KILLSWITCH_DIR", raising=False)
    from grocery import config

    reloaded = importlib.reload(config)
    assert reloaded.PANIC_FILE == reloaded.ROOT / "console" / "PANIC"
    assert reloaded.PAUSE_FILE == reloaded.ROOT / "console" / "PAUSE"
