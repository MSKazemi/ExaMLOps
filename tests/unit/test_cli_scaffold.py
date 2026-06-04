from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cli.main import app

runner = CliRunner()


def test_scaffold_calls_script_with_name():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.__class__.__name__ = "CompletedProcess"
        result = runner.invoke(app, ["scaffold", "DemoAD"])
    assert result.exit_code == 0
    cmd = mock_run.call_args[0][0]
    assert "--name" in cmd
    assert "DemoAD" in cmd
    assert "scaffold_model.py" in " ".join(cmd)


def test_scaffold_passes_task_and_type():
    with patch("subprocess.run") as mock_run:
        result = runner.invoke(app, ["scaffold", "MyModel", "--task", "anomaly_detection", "--type", "classification"])
    assert result.exit_code == 0
    cmd = mock_run.call_args[0][0]
    assert "--task" in cmd and "anomaly_detection" in cmd
    assert "--task-type" in cmd and "classification" in cmd


def test_scaffold_passes_force():
    with patch("subprocess.run") as mock_run:
        result = runner.invoke(app, ["scaffold", "MyModel", "--force"])
    assert result.exit_code == 0
    cmd = mock_run.call_args[0][0]
    assert "--force" in cmd


def test_scaffold_missing_script_shows_error():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = runner.invoke(app, ["scaffold", "DemoAD"])
    assert "not found" in result.output.lower() or result.exit_code != 0
