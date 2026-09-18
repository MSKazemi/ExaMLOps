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

_REPO = Path(__file__).resolve().parents[2]
_CORE = _REPO / "platform" / "cli" / "src" / "examlops"
_TESTS = _REPO / "tests"
# The one file allowed to call sqlite3.connect directly — it *is* the hardening adapter.
_ALLOWED = {_CORE / "resilience" / "db.py"}
_PATTERN = re.compile(r"\bsqlite3\.connect\s*\(")

#: Tests allowed to call ``sqlite3.connect`` directly, with the reason. The rule below is about
#: reaching *platform state* through the seam; an exemption is only legitimate when the call **is**
#: the thing under test rather than a way to read a row.
_TEST_ALLOWED = {
    "tests/unit/test_suite_stores_are_isolated.py": (
        "drives the `sqlite3.connect` audit hook that refuses the checkout's own stores: the call "
        "is the subject, and on Postgres `get_db()` opens no file that could trigger it"
    ),
}


def _python_files(root: Path) -> list[Path]:
    """Every ``.py`` under ``root`` — and proof that there was something to scan.

    A guard that walks a hard-coded path is only as true as that path. If the tree moves (this
    repo has moved it once already, into ``platform/``), ``rglob`` returns nothing, every
    per-file assertion below runs zero times, and the guard reports green while enforcing
    nothing at all. That failure looks exactly like compliance, so the scan asserts it found
    files before anyone is allowed to conclude anything from what it did not find.
    """
    files = [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]
    assert files, (
        f"scanned {root} and found no Python files — the guard's path is stale, not the tree clean"
    )
    return files


def test_core_package_has_no_bare_sqlite_connect():
    offenders: list[str] = []
    for py in _python_files(_CORE):
        if py in _ALLOWED:
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
            if _PATTERN.search(line):
                offenders.append(f"{py.relative_to(_CORE.parents[3])}:{i}: {line.strip()}")
    assert not offenders, (
        "Bare sqlite3.connect() found — route through examlops.resilience.db.connect "
        "(hardened WAL + busy_timeout) instead:\n" + "\n".join(offenders)
    )


def test_tests_reach_the_platform_db_through_the_seam():
    """A test that opens the SQLite file directly only ever proves the SQLite engine.

    Four of them did, and each one failed the moment the suite ran with
    ``EXAMLOPS_DB_BACKEND=postgres`` — not because the platform was broken, but because the test
    was asserting against a file the platform had stopped writing to. Going through
    ``platform_db.get_db()`` makes the same assertion hold on whichever engine is configured.
    """
    offenders: list[str] = []
    for py in _python_files(_TESTS):
        rel = py.relative_to(_REPO).as_posix()
        if py == Path(__file__).resolve() or rel in _TEST_ALLOWED:
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
            if _PATTERN.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, (
        "Tests must reach platform state through platform_db.get_db(), not sqlite3.connect():\n"
        + "\n".join(offenders)
    )


def test_the_exemptions_still_describe_something_real():
    """An exemption outlives the thing it excused, and then it is just a hole."""
    for rel in _TEST_ALLOWED:
        py = _REPO / rel
        assert py.exists(), f"{rel} is gone; drop the exemption"
        text = py.read_text(encoding="utf-8")
        assert _PATTERN.search(text), f"{rel} no longer calls sqlite3.connect; drop the exemption"
        assert "addaudithook" in text or "audit hook" in text, (
            f"{rel} is exempted for driving the connect audit hook and no longer mentions it"
        )
