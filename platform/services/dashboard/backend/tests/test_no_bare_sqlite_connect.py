"""Guard: no bare `sqlite3.connect` in the dashboard backend (Phase 0 item 0.2 / QW1).

The dashboard is a separate app and can't import `examlops.resilience`, so it carries its own
hardened-connection helper in `dbconn.py`. Every platform.db access in the backend must go through
`dbconn.connect(...)` so it uniformly gets WAL + `synchronous=NORMAL` + a `busy_timeout` that waits
out lock contention instead of raising `database is locked` 500s. A bare `sqlite3.connect(...)`
bypasses that hardening. The single sanctioned call site is `dbconn.py` itself — it *is* the adapter.

`tests/` is excluded: test fixtures build throwaway SQLite files directly.
"""

from __future__ import annotations

import re
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
# The one file allowed to call sqlite3.connect directly — it *is* the hardening adapter.
_ALLOWED = {_BACKEND / "dbconn.py"}
_PATTERN = re.compile(r"\bsqlite3\.connect\s*\(")


def test_backend_has_no_bare_sqlite_connect():
    offenders: list[str] = []
    for py in _BACKEND.rglob("*.py"):
        if py in _ALLOWED or "__pycache__" in py.parts or "tests" in py.parts:
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
            if _PATTERN.search(line):
                offenders.append(f"{py.relative_to(_BACKEND)}:{i}: {line.strip()}")
    assert not offenders, (
        "Bare sqlite3.connect() found — route through dbconn.connect "
        "(hardened WAL + busy_timeout) instead:\n" + "\n".join(offenders)
    )
