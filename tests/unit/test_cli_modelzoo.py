"""Tests for exa modelzoo command group."""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from examlops.cli.main import app

runner = CliRunner()


def _mock_get(url: str, token: str = ""):
    if "/modelzoo/status" in url:
        return {
            "models": [
                {
                    "model_id": "JPCP",
                    "status": "stale",
                    "latest_modelzoo_commit": "abc12345",
                    "last_retrain_commit": "def45678",
                    "stale_since": "2026-05-21T10:00:00",
                    "retrain_triggered_at": None,
                },
                {
                    "model_id": "MACK",
                    "status": "current",
                    "latest_modelzoo_commit": "abc12345",
                    "last_retrain_commit": "abc12345",
                    "stale_since": None,
                    "retrain_triggered_at": None,
                },
            ],
            "last_event": {
                "commit_sha": "abc12345",
                "timestamp": "2026-05-21T10:00:00",
                "source": "webhook",
            },
        }
    if "/modelzoo/events" in url:
        return [
            {
                "id": 1,
                "commit_sha": "abc12345",
                "branch": "main",
                "pushed_by": "alice",
                "timestamp": "2026-05-21T10:00:00",
                "source": "webhook",
            },
        ]
    if "/modelzoo/config" in url:
        return {"auto_retrain": False, "poll_interval_seconds": 300, "watch_branch": "main"}
    raise ValueError(f"Unexpected URL: {url}")


def _mock_post(url: str, body: dict, token: str = "", timeout: float = 10.0):
    if "/modelzoo/sync" in url:
        return {"new_commit": True, "commit_sha": "abc12345", "models_marked_stale": 3}
    raise ValueError(f"Unexpected URL: {url}")


@patch("examlops.cli._client.get", side_effect=_mock_get)
def test_modelzoo_status_table(_mock):
    result = runner.invoke(app, ["modelzoo", "status"])
    assert result.exit_code == 0
    assert "JPCP" in result.output
    assert "STALE" in result.output
    assert "MACK" in result.output
    assert "CURRENT" in result.output


@patch("examlops.cli._client.get", side_effect=_mock_get)
def test_modelzoo_status_json(_mock):
    result = runner.invoke(app, ["--json", "modelzoo", "status"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert data[0]["model_id"] == "JPCP"


@patch("examlops.cli._client.get", side_effect=_mock_get)
def test_modelzoo_events_table(_mock):
    result = runner.invoke(app, ["modelzoo", "events"])
    assert result.exit_code == 0
    assert "abc12345" in result.output
    assert "alice" in result.output


@patch("examlops.cli._client.post", side_effect=_mock_post)
def test_modelzoo_sync(_mock):
    result = runner.invoke(app, ["modelzoo", "sync"])
    assert result.exit_code == 0
    assert "new commit" in result.output.lower() or "abc12345" in result.output


def _mock_post_poll_error(url: str, body: dict, token: str = "", timeout: float = 10.0):
    if "/modelzoo/sync" in url:
        return {"new_commit": False, "error": "Temporary failure in name resolution"}
    raise ValueError(f"Unexpected URL: {url}")


@patch("examlops.cli._client.post", side_effect=_mock_post_poll_error)
def test_modelzoo_sync_surfaces_poll_error(_mock):
    """A failed poll must show the error, not 'up-to-date'."""
    result = runner.invoke(app, ["modelzoo", "sync"])
    assert "poll failed" in result.output.lower()
    assert "name resolution" in result.output
    assert "up-to-date" not in result.output.lower()


@patch("examlops.cli._client.get", side_effect=_mock_get)
def test_modelzoo_config_show(_mock):
    result = runner.invoke(app, ["modelzoo", "config"])
    assert result.exit_code == 0
    assert "auto_retrain" in result.output or "false" in result.output.lower()
