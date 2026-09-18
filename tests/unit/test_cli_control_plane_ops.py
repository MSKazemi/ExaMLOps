"""Control-plane capabilities an operator could not reach from `exa` (or the dashboard).

`GET /retrain/{flow_run_id}` answered "is my retrain done?" — only an MCP tool called it, and
`exa retrain` told the operator to "monitor: exa status". `POST /admin/reload` picks up new model
YAML without restarting the control plane — nothing called it. Both are now commands, and so
also reachable from the dashboard's CLI Console.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from examlops.cli import _client
from examlops.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://cp.test:8002")
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "t0ken-for-tests-only")


def test_retrain_status_asks_the_control_plane_about_that_run():
    status = {"flow_run_id": "abc/1", "state": "RUNNING", "state_name": "Running"}
    with patch("examlops.cli.commands.retrain._client.get", return_value=status) as get:
        result = runner.invoke(app, ["--json", "retrain-status", "abc/1"])
    assert result.exit_code == 0, result.output
    url = get.call_args.args[0]
    assert url == "http://cp.test:8002/v1/runs/abc%2F1"  # the id is path-escaped
    assert get.call_args.kwargs["token"] == "t0ken-for-tests-only"
    assert json.loads(result.output)["state"] == "RUNNING"


def test_retrain_status_reports_an_unknown_run_as_an_error():
    err = _client.ClientError("404 Flow run not found")
    with patch("examlops.cli.commands.retrain._client.get", side_effect=err):
        result = runner.invoke(app, ["--json", "retrain-status", "nope"])
    assert result.exit_code == 1
    assert "not found" in json.loads(result.output)["error"].lower()


def test_retrain_points_at_retrain_status():
    from examlops.cli.commands import retrain

    assert "exa retrain-status" in retrain._EXAMPLES_STATUS


def test_production_reload_hot_reloads_the_control_plane_registry():
    reply = {"registry_reloaded": True, "models": ["JPCP", "MACK"], "startup_checks": {"db": "ok"}}
    with patch("examlops.cli.commands.production._client.post", return_value=reply) as post:
        result = runner.invoke(app, ["--json", "production", "reload"])
    assert result.exit_code == 0, result.output
    assert post.call_args.args[0] == "http://cp.test:8002/v1/admin/reload"
    assert post.call_args.kwargs["token"] == "t0ken-for-tests-only"
    payload = json.loads(result.output)
    assert payload["models"] == ["JPCP", "MACK"]


def test_production_reload_needs_a_reachable_control_plane():
    with patch(
        "examlops.cli.commands.production._client.post",
        side_effect=_client.ClientError("connection refused"),
    ):
        result = runner.invoke(app, ["--json", "production", "reload"])
    assert result.exit_code == 1
    assert "hint" in json.loads(result.output)
