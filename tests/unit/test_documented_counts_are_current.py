# tests/unit/test_documented_counts_are_current.py
"""A number in a guide is a claim, and claims about this repository go stale silently.

The testing guide opened with "the unit suite is **2781 tests**" while the suite was 6411 — wrong by
a factor of 2.3, in the first line of a page edited several times that same week. Nobody noticed,
because a stale number reads exactly like a fresh one. The same had happened to "127 tables" (142),
"3388 tests" (6411) and the dashboard's "453/453" (665).

These are not decoration: the testing guide's entire argument — run everything, quickly, rather than
selecting affected tests — rests on how long the suite takes, and a reader who checks the number
against reality and finds it wrong has no reason to trust the argument either.

**This is a drift check, not a pin.** A guard demanding the exact count would fail on every test
added, and a guard that fails on ordinary work is a guard someone deletes. It fails only when a
documented figure has drifted far enough to mislead, which is the point at which someone should
re-measure and re-word.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: How far a documented count may drift before it misleads. Generous on purpose: this must fire
#: when a number is *wrong*, not when the repository has grown normally since it was written.
TOLERANCE = 0.20


def _documented(path: str, pattern: str) -> tuple[int, str]:
    """The number a page claims, and the sentence it sits in (for the failure message)."""
    text = (ROOT / path).read_text(encoding="utf-8")
    match = re.search(pattern, text)
    assert match, f"{path} no longer states this figure — the guard's pattern is stale, not the doc"
    line = text[: match.start()].count("\n") + 1
    return int(match.group(1).replace(",", "").replace(" ", "")), f"{path}:{line}"


def _platform_tables() -> int:
    """Tables `init_db()` creates, counted by creating them in a throwaway datastore."""
    with tempfile.TemporaryDirectory() as tmp:
        code = (
            "import sys\n"
            "sys.path.insert(0, 'platform/cli/src')\n"
            "from examlops.platform_db import get_db, init_db\n"
            "init_db()\n"
            "with get_db() as conn:\n"
            '    row = conn.execute("SELECT count(*) AS n FROM sqlite_master '
            "WHERE type='table'\").fetchone()\n"
            "print(row['n'])\n"
        )
        env = {**os.environ, "PLATFORM_DB": str(Path(tmp) / "count.db")}
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
            timeout=180,
        )  # fmt: skip
        assert out.returncode == 0, out.stderr[-400:]
        return int(out.stdout.strip().splitlines()[-1])


def _collected_unit_tests() -> int:
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/",
            "--collect-only",
            "-q",
            "-p",
            "no:randomly",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    match = re.search(r"(\d+) tests? collected", out.stdout)
    assert match, f"could not read a collected count from pytest: {out.stdout[-300:]}"
    return int(match.group(1))


def _assert_close(claimed: int, actual: int, where: str, what: str) -> None:
    drift = abs(claimed - actual) / max(actual, 1)
    assert drift <= TOLERANCE, (
        f"{where} says {claimed:,} {what}; there are {actual:,} — {drift:.0%} off. Re-measure and "
        "re-word it: a number a reader can check and find wrong costs the surrounding argument its "
        "credibility, which is the only reason to print one."
    )


def test_the_table_count_in_the_guides_is_current():
    actual = _platform_tables()
    for path, pattern in (
        ("docs/guides/architecture.md", r"(\d{2,4}) tables"),
        ("docs/guides/postgres-backend.md", r"\((\d{2,4}) tables, `CREATE TABLE IF NOT EXISTS`\)"),
    ):
        claimed, where = _documented(path, pattern)
        _assert_close(claimed, actual, where, "tables")


def test_the_unit_suite_size_in_the_guides_is_current():
    actual = _collected_unit_tests()
    for path, pattern in (
        ("docs/guides/testing.md", r"unit suite is \*\*([\d,]+) tests\*\*"),
        ("docs/guides/developer-onboarding.md", r"unit suite \(\*\*([\d,]+) tests\*\*\)"),
    ):
        claimed, where = _documented(path, pattern)
        _assert_close(claimed, actual, where, "tests")


def test_the_tolerance_is_a_drift_check_not_a_pin():
    """If this is ever tightened to near-zero the guard starts failing on ordinary work, and a
    guard that fails on ordinary work is one somebody deletes."""
    assert 0.10 <= TOLERANCE <= 0.30
