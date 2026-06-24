from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cli.main import app

runner = CliRunner()


def test_pipeline_deploy_calls_deploy_script():
    with patch("subprocess.run") as mock_run:
        result = runner.invoke(app, ["pipeline", "deploy"])
    assert result.exit_code == 0
    cmd = mock_run.call_args[0][0]
    assert "deploy.py" in " ".join(cmd)


def test_pipeline_deploy_no_schedule():
    with patch("subprocess.run") as mock_run:
        result = runner.invoke(app, ["pipeline", "deploy", "--no-schedule"])
    assert result.exit_code == 0
    cmd = mock_run.call_args[0][0]
    assert "--no-schedule" in cmd


def test_pipeline_deploy_model():
    with patch("subprocess.run") as mock_run:
        result = runner.invoke(app, ["pipeline", "deploy", "--model", "JPCP"])
    assert result.exit_code == 0
    cmd = mock_run.call_args[0][0]
    assert "--model" in cmd and "JPCP" in cmd


def test_pipeline_deploy_with_registry_and_env():
    with patch("subprocess.run") as mock_run:
        result = runner.invoke(
            app,
            ["pipeline", "deploy", "--registry", "pipelines/model_registry.yaml", "--env", "prod"],
        )
    assert result.exit_code == 0
    cmd = mock_run.call_args[0][0]
    assert "--registry" in cmd and "--env" in cmd and "prod" in cmd


def test_pipeline_export_registry_calls_generator():
    with patch("subprocess.run") as mock_run:
        result = runner.invoke(app, ["pipeline", "export-registry"])
    assert result.exit_code == 0
    cmd = mock_run.call_args[0][0]
    assert "pipeline_generator.py" in " ".join(cmd)
    assert "--export-registry" in cmd


def test_pipeline_validate_runs_registry_integrity_guard():
    with patch("subprocess.run") as mock_run:
        result = runner.invoke(app, ["pipeline", "validate"])
    assert result.exit_code == 0
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "pytest" in joined
    assert "tests/unit/test_registry_integrity.py" in cmd
    assert "yaml" in cmd


def test_pipeline_run_passes_dataset_backend_and_dummy():
    with patch("subprocess.run") as mock_run:
        result = runner.invoke(
            app,
            [
                "pipeline",
                "run",
                "--model",
                "JPCP",
                "--dataset",
                "PM100Dataset",
                "--backend",
                "minio",
                "--dummy",
            ],
        )
    assert result.exit_code == 0
    cmd = mock_run.call_args[0][0]
    assert "--model" in cmd and "JPCP" in cmd
    assert "--dataset" in cmd and "PM100Dataset" in cmd
    assert "--backend" in cmd and "minio" in cmd
    assert "--dummy" in cmd
