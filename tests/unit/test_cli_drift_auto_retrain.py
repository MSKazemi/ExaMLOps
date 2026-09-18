from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops.cli.main import app
from examlops.data.drift import set_corruption_baseline, set_input_baseline, write_input_snapshot
from examlops.platform_db import (
    get_drift_auto_retrain,
    init_db,
    set_drift_baseline,
    write_drift_snapshot,
)


def seed_data_drift_evidence(model: str, preds: list[float]) -> None:
    """Give `model` the *second* axis ADR 0114 requires before an autonomous retrain.

    Prediction drift alone now classifies as `undetermined`, so a test that means "this model
    is genuinely drifting" has to say so on both axes: inputs 4σ from their baseline, and a
    corruption baseline that matches the predictions.
    """
    from examlops.corruption import corruption_stats

    set_corruption_baseline(model, corruption_stats(preds))
    set_input_baseline(
        model,
        {
            "norm_mean": 1.0,
            "norm_mean_std": 0.1,
            "mean_mean": 0.0,
            "mean_mean_std": 0.1,
            "std_mean": 1.0,
            "std_mean_std": 0.1,
        },
    )
    for _ in range(50):
        write_input_snapshot(model, "Production", 1.4, 0.0, 1.0, None)


runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_db(tmp_path, monkeypatch):
    os.environ["PLATFORM_DB"] = str(tmp_path / "test.db")
    init_db()
    # ADR 0113: an autonomous retrain is refused unless the platform can name the version a
    # rollback would restore, which needs MLflow. These tests are about drift behaviour, so the
    # precondition is supplied; the gate is exercised in test_rollback_registry.py.
    from examlops.cli.commands import drift as _drift_cmd

    monkeypatch.setattr(_drift_cmd, "_alias_version", lambda model, alias="Production": "4")
    yield
    del os.environ["PLATFORM_DB"]


def test_auto_retrain_enable():
    result = runner.invoke(
        app, ["drift", "auto-retrain", "enable", "JPCP", "--dataset", "PM100Dataset"]
    )
    assert result.exit_code == 0, result.output
    assert "enabled" in result.output.lower()
    cfg = get_drift_auto_retrain("JPCP")
    assert cfg is not None
    assert cfg["enabled"] == 1
    assert cfg["dataset_name"] == "PM100Dataset"
    assert cfg["min_z_score"] == 3.0


def test_auto_retrain_enable_custom_z():
    result = runner.invoke(app, ["drift", "auto-retrain", "enable", "JPCP", "--min-z", "2.5"])
    assert result.exit_code == 0, result.output
    cfg = get_drift_auto_retrain("JPCP")
    assert cfg["min_z_score"] == 2.5


def test_auto_retrain_disable():
    runner.invoke(app, ["drift", "auto-retrain", "enable", "JPCP"])
    result = runner.invoke(app, ["drift", "auto-retrain", "disable", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "disabled" in result.output.lower()
    cfg = get_drift_auto_retrain("JPCP")
    assert cfg["enabled"] == 0


def test_auto_retrain_disable_not_found():
    result = runner.invoke(app, ["drift", "auto-retrain", "disable", "UNKNOWN"])
    assert "No auto-retrain config" in result.output


def test_auto_retrain_status_empty():
    result = runner.invoke(app, ["drift", "auto-retrain", "status"])
    assert result.exit_code == 0, result.output
    assert "No auto-retrain" in result.output


def test_auto_retrain_status_shows_config():
    runner.invoke(
        app, ["drift", "auto-retrain", "enable", "JPCP", "--min-z", "2.5", "--cooldown", "1800"]
    )
    result = runner.invoke(app, ["drift", "auto-retrain", "status"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    assert "2.5" in result.output
    assert "1800" in result.output


def test_trigger_no_config():
    result = runner.invoke(app, ["drift", "trigger"])
    assert result.exit_code == 0, result.output
    assert "No models with auto-retrain enabled" in result.output


def test_trigger_dry_run_below_threshold():
    import random

    random.seed(42)
    for _ in range(20):
        write_drift_snapshot("JPCP", "Production", random.gauss(0, 0.1), None)
    set_drift_baseline("JPCP", {"mean": 0.0, "std": 1.0, "n": 100.0})
    seed_data_drift_evidence("JPCP", [10.0] * 20)
    runner.invoke(app, ["drift", "auto-retrain", "enable", "JPCP", "--min-z", "3.0"])
    result = runner.invoke(app, ["drift", "trigger", "--dry-run"])
    assert result.exit_code == 0, result.output
    # z is very small, should be in skipped
    assert (
        "Skipped" in result.output
        or "below" in result.output.lower()
        or "no retrains" in result.output.lower()
    )


def test_trigger_dry_run_above_threshold():
    # live mean=10, baseline mean=0, std=1 → z=10 ≥ 3.0
    for _ in range(20):
        write_drift_snapshot("JPCP", "Production", 10.0, None)
    set_drift_baseline("JPCP", {"mean": 0.0, "std": 1.0, "n": 100.0})
    seed_data_drift_evidence("JPCP", [10.0] * 20)
    runner.invoke(app, ["drift", "auto-retrain", "enable", "JPCP", "--min-z", "3.0"])
    result = runner.invoke(app, ["drift", "trigger", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    assert (
        "dry-run" in result.output.lower()
        or "would retrain" in result.output.lower()
        or "Triggered" in result.output
    )


def test_trigger_fires_retrain(tmp_path):
    for _ in range(20):
        write_drift_snapshot("JPCP", "Production", 10.0, None)
    set_drift_baseline("JPCP", {"mean": 0.0, "std": 1.0, "n": 100.0})
    seed_data_drift_evidence("JPCP", [10.0] * 20)
    runner.invoke(app, ["drift", "auto-retrain", "enable", "JPCP", "--min-z", "3.0"])
    mock_result = {
        "command_id": "v1:retrain:x",
        "state": "succeeded",
        "result": {"flow_run_id": "test-flow-123"},
        "status_url": "/v1/commands/v1:retrain:x",
    }  # POST /v1/retrain, dispatched (plan P1.6c)
    with patch("examlops.cli._client.post", return_value=mock_result) as mock_post:
        result = runner.invoke(app, ["drift", "trigger"])
    assert result.exit_code == 0, result.output
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert call_args[0][0].endswith("/v1/retrain")
    assert call_args[0][1]["model_name"] == "JPCP"
    assert "JPCP" in result.output
