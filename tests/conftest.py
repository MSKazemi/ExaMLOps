"""Shared pytest fixtures."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_STARTED: pytest.StashKey[float] = pytest.StashKey()
sys.path.insert(0, str(REPO_ROOT / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolate_postgres_state():
    """Give every test an empty platform database when running on Postgres.

    The mechanism lives in :mod:`examlops.storage.testing` because the dashboard's suite — a
    separate app with its own connection adapter — needs exactly the same thing, and a copy in
    two conftests would drift. No-op on SQLite, where each test gets its own ``tmp_path`` file.
    """
    from examlops.storage.testing import postgres_isolation

    yield from postgres_isolation()


@pytest.fixture(autouse=True)
def _reset_cli_output_modes():
    """Return the CLI's output globals to their defaults before every test.

    ``examlops.cli.main``'s root callback sets ``json_mode``/``quiet_mode``/``verbose_mode``/
    ``yes_mode``/``output_format`` on the module and never restores them, so a single
    ``runner.invoke(app, ["-q", ...])`` leaves quiet mode on for the rest of the process. Tests
    that invoke a *sub*-app never run that callback, so nothing clears it for them.

    The failure that makes this worth a fixture is silent: a leaked ``quiet_mode`` suppresses
    exactly the output an "it must not print X" assertion is looking for, so the test passes
    without evaluating anything. Nothing in the suite is losing that way today — checked, and the
    absence-assertions downstream of the one live leak read subprocess stdout and SQL strings, not
    ``_output`` — but it is order-dependent, and the next such test would be green on arrival.
    """
    from examlops.cli import _output

    _output.json_mode = False
    _output.quiet_mode = False
    _output.verbose_mode = False
    _output.yes_mode = False
    _output.output_format = "table"
    yield


def pytest_configure(config: pytest.Config) -> None:
    """Stamp when the session started, so the summary can tell if the tree moved under it."""
    config.stash[_STARTED] = time.time()


def pytest_terminal_summary(terminalreporter, exitstatus, config: pytest.Config) -> None:
    """Say so when a source file changed while the suite was running.

    A pytest run reads ``conftest.py`` once at startup and each test module once at collection, so
    a file saved a few seconds into a twenty-minute run produces a result that belongs to no
    version of the tree: part of the run saw the old file, the rest saw the new one. This repo has
    a second writer in it often enough that the failure is not hypothetical — one run reported
    ``test_the_next_test_starts_from_the_defaults_anyway`` red because the autouse fixture that
    test exists to check was written into ``conftest.py`` three seconds after collection began.
    Without this line the only way to find that out is to compare mtimes against the run window by
    hand, long after the log has scrolled away.

    Reported, never enforced: the run's own exit status is untouched, because a mid-run edit does
    not make the result wrong, only unreliable.
    """
    started = config.stash.get(_STARTED, None)
    if started is None:
        return
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "*.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return
    moved = []
    for name in listing.split("\0"):
        if not name:
            continue
        try:
            if (REPO_ROOT / name).stat().st_mtime > started:
                moved.append(name)
        except OSError:
            continue
    if not moved:
        return
    terminalreporter.write_sep("=", "tree changed during this run", yellow=True)
    for name in sorted(moved)[:10]:
        terminalreporter.write_line(f"  {name}", yellow=True)
    if len(moved) > 10:
        terminalreporter.write_line(f"  … and {len(moved) - 10} more", yellow=True)
    terminalreporter.write_line(
        "These were written after collection started, so this result may mix two versions of the "
        "tree. Re-run on a settled tree before trusting it.",
        yellow=True,
    )
