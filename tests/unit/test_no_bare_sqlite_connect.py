"""CI guard: no bare `sqlite3.connect` in the core `examlops` package (Phase 0 item 0.2 / QW1).

Every SQLite access in the core package must go through `examlops.resilience.db.connect` (or the
`platform_db.get_db` / `_immediate_write` helpers built on it), so it uniformly gets WAL +
`synchronous=NORMAL` + a `busy_timeout` that waits out lock contention instead of raising
`database is locked`. A bare `sqlite3.connect(...)` bypasses that hardening and reintroduces the
lock-storm/500 failures. The single sanctioned call site is the resilience adapter itself.
"""

from __future__ import annotations

import re
from pathlib import Path

_CORE = Path(__file__).resolve().parents[2] / "platform" / "cli" / "src" / "examlops"
# The one file allowed to call sqlite3.connect directly — it *is* the hardening adapter.
_ALLOWED = {_CORE / "resilience" / "db.py"}
_PATTERN = re.compile(r"\bsqlite3\.connect\s*\(")


def test_core_package_has_no_bare_sqlite_connect():
    offenders: list[str] = []
    for py in _CORE.rglob("*.py"):
        if py in _ALLOWED or "__pycache__" in py.parts:
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
            if _PATTERN.search(line):
                offenders.append(f"{py.relative_to(_CORE.parents[3])}:{i}: {line.strip()}")
    assert not offenders, (
        "Bare sqlite3.connect() found — route through examlops.resilience.db.connect "
        "(hardened WAL + busy_timeout) instead:\n" + "\n".join(offenders)
    )
