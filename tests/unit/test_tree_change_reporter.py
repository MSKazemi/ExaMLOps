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

from tests.unit._guard_deps import require_binary

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
    # Without this, a git-less environment fails here as a bare `assert False` — the reporter
    # produced no lines, and nothing says why. The message is the point.
    require_binary("git", "the tree-change reporter can name the files that moved")
    reporter = _run(started=0.0)
    assert any("tree changed during this run" in line for line in reporter.lines)
    assert any("Re-run on a settled tree" in line for line in reporter.lines)


def test_a_settled_tree_says_nothing():
    """A stamp far in the future: nothing was written after it, so the summary stays quiet."""
    # "Quiet" is only the right expectation when the check could run at all: without git the
    # reporter now (correctly) prints its could-not-run banner instead.
    require_binary("git", "a settled tree really does produce no banner")
    reporter = _run(started=32503680000.0)  # 3000-01-01
    assert reporter.lines == []


def test_no_stamp_is_not_an_error():
    """``pytest_configure`` may not have run (a plugin ordering change) — degrade to silence."""
    assert _run(started=None).lines == []


def test_a_reporter_that_cannot_run_says_so_rather_than_going_quiet(monkeypatch):
    """The one reading that must never be available is "no banner, therefore settled".

    Silence is this reporter's only success signal, so a failure that produces silence is
    indistinguishable from a clean tree — and `git` is genuinely absent from some images that
    run this suite. Make `git ls-files` unrunnable and the summary must still speak.
    """
    real = _conftest.subprocess.run

    def _no_git(cmd, *a, **kw):
        if cmd and cmd[0] == "git":
            raise FileNotFoundError(2, "No such file or directory: 'git'")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(_conftest.subprocess, "run", _no_git)
    reporter = _run(started=0.0)
    assert any("tree-change check did not run" in line for line in reporter.lines)
    assert any("Absence of a warning is not evidence" in line for line in reporter.lines)
