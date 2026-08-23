"""The mid-run tree-change reporter in ``tests/conftest.py`` must be able to fire.

A reporter that never speaks is worse than none: it reads as "the tree was settled" on every run.
These tests drive the hook directly with a stamp from the past and from the future, because the
real trigger — a file written by another process seconds into a twenty-minute run — cannot be
staged reliably inside a two-second test.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "_note_conftest", REPO_ROOT / "tests" / "conftest.py"
)
assert _spec and _spec.loader
_conftest = importlib.util.module_from_spec(_spec)
sys.modules["_note_conftest"] = _conftest
_spec.loader.exec_module(_conftest)


class _Reporter:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_sep(self, _char: str, title: str, **_kw: object) -> None:
        self.lines.append(title)

    def write_line(self, line: str, **_kw: object) -> None:
        self.lines.append(line)


def _run(started: float | None) -> _Reporter:
    class _Config:
        def __init__(self) -> None:
            self.stash = pytest.Stash()

    cfg = _Config()
    if started is not None:
        cfg.stash[_conftest._STARTED] = started
    reporter = _Reporter()
    _conftest.pytest_terminal_summary(reporter, 0, cfg)
    return reporter


def test_a_tree_written_after_the_run_started_is_reported():
    """Every tracked ``.py`` predates a stamp from the future, so all of them are 'changed'."""
    reporter = _run(started=0.0)
    assert any("tree changed during this run" in line for line in reporter.lines)
    assert any("Re-run on a settled tree" in line for line in reporter.lines)


def test_a_settled_tree_says_nothing():
    """A stamp far in the future: nothing was written after it, so the summary stays quiet."""
    reporter = _run(started=32503680000.0)  # 3000-01-01
    assert reporter.lines == []


def test_no_stamp_is_not_an_error():
    """``pytest_configure`` may not have run (a plugin ordering change) — degrade to silence."""
    assert _run(started=None).lines == []
