"""Guard: no bare `sqlite3.connect` in the dashboard backend (Phase 0 item 0.2 / QW1).

The dashboard is a separate app and can't import `examlops.resilience`, so it carries its own
hardened-connection helper in `dbconn.py`. Every platform.db access in the backend must go through
`dbconn.connect(...)` so it uniformly gets WAL + `synchronous=NORMAL` + a `busy_timeout` that waits
out lock contention instead of raising `database is locked` 500s. A bare `sqlite3.connect(...)`
bypasses that hardening. The single sanctioned call site is `dbconn.py` itself — it *is* the adapter.

This now covers `tests/` too. Those fixtures used to open throwaway SQLite files directly,
which was harmless while SQLite was the only engine — but under `EXAMLOPS_DB_BACKEND=postgres`
it meant a test seeded one store and the router it was testing read a different one, so the
test asserted on rows nobody had written. Going through `connect()` is what makes a dashboard
test engine-neutral, so bypassing it is now a failure rather than a convention.
"""

from __future__ import annotations

import re
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
# `dbconn.py` is allowed to call it directly — it *is* the hardening adapter. This file is
# allowed because it has to spell the pattern out to search for it; excluding it by path is
# clearer than obfuscating the literal.
_ALLOWED = {_BACKEND / "dbconn.py", Path(__file__).resolve()}
_PATTERN = re.compile(r"\bsqlite3\.connect\s*\(")


def test_backend_has_no_bare_sqlite_connect():
    offenders: list[str] = []
    for py in _BACKEND.rglob("*.py"):
        if py in _ALLOWED or "__pycache__" in py.parts:
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
            if _PATTERN.search(line):
                offenders.append(f"{py.relative_to(_BACKEND)}:{i}: {line.strip()}")
    assert not offenders, (
        "Bare sqlite3.connect( found — route through dbconn.connect "
        "(hardened WAL + busy_timeout) instead:\n" + "\n".join(offenders)
    )
