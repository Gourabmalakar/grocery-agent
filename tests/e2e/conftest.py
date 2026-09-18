"""
The `live` fixture (real mockstore + console processes on isolated ports)
used to live here. It moved up to tests/conftest.py so tests/sentinel/ can
reuse it too, without a second copy of the process-spinning logic — see that
file's comment for the full rationale. Nothing else belonged in this file,
so it's now just this note; pytest fixtures in the top-level conftest are
automatically visible to every test under tests/, this directory included.
"""
