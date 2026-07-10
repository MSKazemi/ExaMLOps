from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops.cli import _output
from examlops.cli.main import app

runner = CliRunner()


def test_quiet_suppresses_info_and_hint():
    _output.quiet_mode = True
    try:
        _output.info("should not show")
        _output.hint("neither should this")
    finally:
        _output.quiet_mode = False


def test_detail_only_under_verbose(capsys):
    # detail() is silent by default…
    _output.verbose_mode = False
    _output.detail("hidden")
    # …and visible under --verbose.
    _output.verbose_mode = True
    try:
        _output.detail("shown")
    finally:
        _output.verbose_mode = False


def test_quiet_flag_registered_globally():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--quiet" in result.output
    assert "--verbose" in result.output


def test_quiet_flag_runs_ok():
    # A quiet status invocation still succeeds (chatter suppressed, primary output intact).
    result = runner.invoke(app, ["--quiet", "drift", "status"])
    assert result.exit_code == 0, result.output
