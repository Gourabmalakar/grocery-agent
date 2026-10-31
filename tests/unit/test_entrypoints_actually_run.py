"""The command a human types must work, not just the module a test imports.

`python -m worker.run <id>` crashed with NameError while all 261 tests were
green. Three helpers had been appended BELOW the `if __name__ == "__main__":`
guard, so running the module executed main() before they were defined — while
importing it defined everything first and hid the bug completely. Every test
calls _plan_and_propose directly, so nothing exercised the entry point itself.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_ENTRYPOINTS = ["worker/run.py", "console/app.py", "mockstore/app.py",
                "grocery/adapters/zepto_mcp.py", "grocery/adapters/swiggy_mcp.py",
                "grocery/adapters/blinkit_browser.py", "grocery/adapters/jiomart_api.py"]


@pytest.mark.parametrize("relative", _ENTRYPOINTS)
def test_nothing_is_defined_below_the_main_guard(relative):
    """Anything after `if __name__ == "__main__":` does not exist yet when that
    block runs. A module-level def down there is invisible to every import-based
    test and fatal to the command itself."""
    tree = ast.parse((_REPO / relative).read_text())
    guard_line = None
    for node in tree.body:
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"):
            guard_line = node.lineno
    if guard_line is None:
        return
    late = [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign))
            and n.lineno > guard_line]
    assert not late, (
        f"{relative} defines {[getattr(n, 'name', '<assignment>') for n in late]} below its "
        "__main__ guard — imports hide this, the command does not"
    )


def test_the_worker_entrypoint_starts_and_reports_a_missing_order():
    """Actually RUN it. An order id that does not exist is the cheapest input
    that still forces the whole module to import and main() to execute."""
    result = subprocess.run(
        [sys.executable, "-m", "worker.run", "order_does_not_exist"],
        cwd=_REPO, capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "MERCHANT_BACKEND": "mock", "LLM_BACKEND": "off", "GEMINI_API_KEY": "",
             "DATA_DIR": str(_REPO / "data")},
    )
    combined = result.stdout + result.stderr
    assert "NameError" not in combined, combined[-800:]
    assert "is not defined" not in combined, combined[-800:]


def test_the_worker_usage_message_works():
    result = subprocess.run([sys.executable, "-m", "worker.run"], cwd=_REPO,
                            capture_output=True, text=True, timeout=120,
                            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
                                 "MERCHANT_BACKEND": "mock", "LLM_BACKEND": "off"})
    assert "usage: python -m worker.run" in result.stdout
    assert result.returncode == 1
