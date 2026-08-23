"""Shared pytest fixtures."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
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
