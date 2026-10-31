"""No test module may import grocery/console/worker/bot at module level.

HOW THIS WENT WRONG, which is why it is now a test. tests/conftest.py's
isolated_data_dir fixture re-points DATA_DIR at a tmp_path and then PURGES
grocery/console/worker/bot from sys.modules so they re-read it. That only works
if the import happens AFTER the fixture runs — i.e. inside the test body.

A module-level `from grocery.ledger import store as ledger_store` is executed at
collection time, before any fixture, and binds the module built from the REAL
config. Every test in that file then reads and writes the production ledger.
tests/adversarial/test_payment_safety.py did exactly that on 2026-10-13 and
wrote a ₹300 spend row plus six pay-intent rows into data/ledger.db — a fake
spend that would have counted against the real daily cap.

The existing conftest comment warns about the same hazard for worker.llm making
billed model calls. This makes it checkable rather than remembered.
"""
from __future__ import annotations

import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]

# NARROW ON PURPOSE. Fourteen test modules import grocery.compare,
# grocery.adapters.base, worker.auditor and friends at module level and are
# perfectly safe: they are pure functions over data passed in, and binding a
# stale config object costs nothing because they never read a path.
#
# What is NOT safe is binding a module that PERSISTS something. Those write to
# whatever DATA_DIR was set when they were imported, which for a module-level
# import is the real one. A blanket ban would be a large refactor of harmless
# code and would train people to silence it; this list is the actual hazard.
_GUARDED = (
    "grocery.ledger",      # the spend ledger and the caps — the one that bit
    "grocery.audit",       # append-only chains; a test write corrupts them
    "grocery.mandates",    # signs with, and can create, the real keypair
    "grocery.killswitch",  # PANIC/PAUSE are repo-relative; a test trip is real
    "console.approval",    # loads and SAVES real order files
)

_EXEMPT = {"conftest.py"}


def _module_level_imports(tree: ast.Module) -> list[tuple[str, int]]:
    """Only the top level — an import inside a function or a fixture is exactly
    the pattern we want people to use."""
    found = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(node.module.startswith(g) for g in _GUARDED):
                found.append((node.module, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.startswith(g) for g in _GUARDED):
                    found.append((alias.name, node.lineno))
    return found


def test_no_test_module_binds_the_real_config_at_import_time():
    offenders = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        if path.name in _EXEMPT:
            continue
        for module, line in _module_level_imports(ast.parse(path.read_text())):
            offenders.append(f"{path.relative_to(_TESTS.parent)}:{line} imports {module}")
    assert not offenders, (
        "these modules import STATE-WRITING production code before "
        "isolated_data_dir can re-point DATA_DIR, so they read and write the "
        "REAL data directory. Move the import inside the test function:\n  "
        + "\n  ".join(offenders))
