from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops.cli.main import app
from examlops.platform_db import get_db, init_db

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    os.environ["PLATFORM_DB"] = str(tmp_path / "test.db")
    init_db()
    # Ensure HPO tables are created
    from examlops.cli.commands.hpo_cmd import _ensure_hpo_tables

    _ensure_hpo_tables()
    yield
    os.environ.pop("PLATFORM_DB", None)


def _dispatched(run_id: str) -> dict:
    """What POST /v1/retrain answers once the command is dispatched (plan P1.6c)."""
    return {
        "command_id": f"v1:retrain:{run_id}",
        "state": "succeeded",
        "result": {"flow_run_id": run_id, "deployment": "nightly"},
        "status_url": f"/v1/commands/v1:retrain:{run_id}",
    }


# Test 1: hpo status with no studies → empty message
def test_hpo_status_empty():
    result = runner.invoke(app, ["pipeline", "hpo", "status"])
    assert result.exit_code == 0, result.output
    assert "no hpo studies" in result.output.lower() or "No HPO studies" in result.output


# Test 2: hpo start JPCP with mocked control plane → study created
def test_hpo_start_creates_study():
    mock_response = _dispatched("abc123")
    with patch("examlops.cli._client.post", return_value=mock_response) as mock_post:
        result = runner.invoke(app, ["pipeline", "hpo", "start", "JPCP", "--trials", "20"])
    assert result.exit_code == 0, result.output
    # The control plane's retrain contract. The body used to be {"model", "dataset",
    # "hpo_trials"}, which the control plane refuses with 422 — this mock accepted it.
    url, body = mock_post.call_args[0][:2]
    assert url.endswith("/v1/retrain")
    assert body == {"model_name": "JPCP", "dataset_name": "PM100Dataset", "is_dummy": False}
    assert "abc123" in result.output
    assert "20" in result.output

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM hpo_studies WHERE model='JPCP'").fetchall()
    assert len(rows) == 1
    assert rows[0]["flow_run_id"] == "abc123"
    assert rows[0]["n_trials"] == 20
    assert rows[0]["status"] == "pending"


# Test 3: hpo status shows the created study
def test_hpo_status_shows_study():
    mock_response = _dispatched("xyz789")
    with patch("examlops.cli._client.post", return_value=mock_response):
        runner.invoke(
            app, ["pipeline", "hpo", "start", "JPCP", "--trials", "30", "--metric", "rmse"]
        )

    result = runner.invoke(app, ["pipeline", "hpo", "status"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    assert "xyz789" in result.output
    assert "30" in result.output or "rmse" in result.output


# Test 4: hpo record records a trial
def test_hpo_record_trial():
    mock_response = _dispatched("flow-record-test")
    with patch("examlops.cli._client.post", return_value=mock_response):
        runner.invoke(app, ["pipeline", "hpo", "start", "JPCP"])

    params = json.dumps({"n_estimators": 100})
    result = runner.invoke(
        app,
        [
            "pipeline",
            "hpo",
            "record",
            "JPCP",
            "--trial",
            "1",
            "--params",
            params,
            "--value",
            "10.5",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Trial 1 recorded" in result.output

    with get_db() as conn:
        trial_rows = conn.execute("SELECT * FROM hpo_trials WHERE trial_num=1").fetchall()
    assert len(trial_rows) == 1
    assert trial_rows[0]["value"] == pytest.approx(10.5)
    assert trial_rows[0]["params_json"] == params


# Test 5: hpo start with HTTP error → exit 1 and error message
def test_hpo_start_http_error():
    from examlops.cli._client import ClientError

    with patch("examlops.cli._client.post", side_effect=ClientError("connection refused")):
        result = runner.invoke(app, ["pipeline", "hpo", "start", "JPCP"])
    assert result.exit_code != 0
    assert "Failed to start HPO study" in result.output or "connection refused" in result.output


# Test 6: hpo record with no existing study → exit 1
def test_hpo_record_no_study():
    params = json.dumps({"lr": 0.01})
    result = runner.invoke(
        app,
        [
            "pipeline",
            "hpo",
            "record",
            "UNKNOWNMODEL",
            "--trial",
            "1",
            "--params",
            params,
            "--value",
            "5.0",
        ],
    )
    assert result.exit_code != 0
    assert "No HPO study found" in result.output or "no hpo study" in result.output.lower()


# Test 7: hpo status filtered by model
def test_hpo_status_filtered_by_model():
    mock_response = _dispatched("flow-a")
    with patch("examlops.cli._client.post", return_value=mock_response):
        runner.invoke(app, ["pipeline", "hpo", "start", "JPCP"])
        runner.invoke(app, ["pipeline", "hpo", "start", "OTHERMODEL"])

    result = runner.invoke(app, ["pipeline", "hpo", "status", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
