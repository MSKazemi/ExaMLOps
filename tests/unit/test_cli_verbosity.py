from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops.cli import _output
from examlops.cli.main import app

runner = CliRunner()


def test_quiet_suppresses_info_and_hint(capsys):
    """Both halves, because only asserting the silence lets a no-op emitter pass.

    This test called ``info()`` and ``hint()`` under quiet mode and asserted nothing at all: it
    passed whether quiet suppressed the output, printed it, or the emitters did nothing in any
    mode. A test named for a behaviour has to be able to fail when that behaviour is absent, so
    it now proves the message *is* printed normally and *is not* printed under ``--quiet``.
    """
    _output.info("info-marker")
    _output.hint("hint-marker")
    loud = capsys.readouterr().out
    assert "info-marker" in loud
    assert "hint-marker" in loud

    _output.quiet_mode = True
    try:
        _output.info("info-marker")
        _output.hint("hint-marker")
    finally:
        _output.quiet_mode = False
    assert capsys.readouterr().out == ""


def test_detail_only_under_verbose(capsys):
    """The fixture was already here; nothing ever read it.

    ``capsys`` was requested and then never consulted, so the test asserted neither half of the
    claim in its name — ``detail()`` could have printed always, or never, and this still passed.
    """
    # detail() is silent by default…
    _output.verbose_mode = False
    _output.detail("hidden")
    assert capsys.readouterr().out == ""
    # …and visible under --verbose.
    _output.verbose_mode = True
    try:
        _output.detail("shown")
    finally:
        _output.verbose_mode = False
    assert "shown" in capsys.readouterr().out


def test_quiet_flag_registered_globally():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--quiet" in result.output
    assert "--verbose" in result.output


def test_quiet_flag_runs_ok():
    # A quiet status invocation still succeeds (chatter suppressed, primary output intact).
    result = runner.invoke(app, ["--quiet", "drift", "status"])
    assert result.exit_code == 0, result.output


def test_a_root_invocation_leaks_its_flags_into_the_process():
    """The root callback sets module globals and never restores them.

    Documented as a test rather than a comment because it is the reason the autouse fixture in
    ``tests/conftest.py`` exists: after this invocation the process is in quiet mode, and only a
    later root invocation *without* ``-q`` would clear it. Any test in between that invokes a
    sub-app and asserts something was not printed would be asserting against a muted emitter.
    """
    runner.invoke(app, ["--quiet", "config", "contexts"])
    assert _output.quiet_mode is True, "the callback sets the global — this is the leak, not a bug"


def test_the_next_test_starts_from_the_defaults_anyway():
    """Ordered directly after the leak above; green only because the fixture undoes it.

    Delete ``_reset_cli_output_modes`` from ``tests/conftest.py`` and this fails.
    """
    assert _output.quiet_mode is False
    assert _output.json_mode is False
    assert _output.verbose_mode is False
    assert _output.output_format == "table"
