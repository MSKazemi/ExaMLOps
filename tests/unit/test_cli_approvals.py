from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cli.main import app

runner = CliRunner()

FAKE_APPROVALS = [
    {
        "id": "u1",
        "model_id": "JPCP",
        "commit_sha": "abc1234",
        "commit_msg": "fix model",
        "changed_files": ["modelzoo/x.py"],
        "status": "pending",
        "prefect_run_id": None,
        "reject_reason": None,
        "requested_at": "2026-05-21T10:00:00",
        "resolved_at": None,
    }
]


def test_approvals_list():
    with patch("examlops.cli.commands.approvals._client.get", return_value=FAKE_APPROVALS):
        result = runner.invoke(app, ["approvals", "list"])
    assert result.exit_code == 0
    assert "JPCP" in result.output


def test_approvals_list_json():
    with patch("examlops.cli.commands.approvals._client.get", return_value=FAKE_APPROVALS):
        result = runner.invoke(app, ["--json", "approvals", "list"])
    assert result.exit_code == 0
    assert "JPCP" in result.output


def test_approvals_approve():
    with patch(
        "examlops.cli.commands.approvals._client.post",
        return_value={"flow_run_id": "run-123", "model_id": "JPCP"},
    ):
        # approve fires training — now guarded by a confirmation prompt.
        result = runner.invoke(app, ["--yes", "approvals", "approve", "JPCP"])
    assert result.exit_code == 0
    assert "JPCP" in result.output


def test_approvals_approve_dry_run_fires_nothing():
    with patch("examlops.cli.commands.approvals._client.post") as mock_post:
        result = runner.invoke(app, ["approvals", "approve", "JPCP", "--dry-run"])
    assert result.exit_code == 0
    assert "dry run" in result.output.lower()
    mock_post.assert_not_called()


def test_approvals_approve_abort_on_decline():
    with patch("examlops.cli.commands.approvals._client.post") as mock_post:
        result = runner.invoke(app, ["approvals", "approve", "JPCP"], input="n\n")
    assert result.exit_code == 0
    assert "aborted" in result.output.lower()
    mock_post.assert_not_called()


def test_approvals_reject():
    with patch(
        "examlops.cli.commands.approvals._client.post",
        return_value={"model_id": "JPCP", "status": "rejected"},
    ):
        result = runner.invoke(app, ["--yes", "approvals", "reject", "JPCP", "--reason", "bad"])
    assert result.exit_code == 0
