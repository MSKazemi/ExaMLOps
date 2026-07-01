from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cli.main import app

runner = CliRunner()

_FAKE_SERVICES = {
    "control_plane": {"ok": True},
    "mlflow": {"ok": True},
    "prefect": {"ok": True, "active_runs": 0},
    "ray_serve": {"ok": True, "models": []},
    "dashboard": {"ok": True},
}
FAKE_STATUS = {
    "status": "ok",
    "pending_approvals": 2,
    "auth_configured": True,
    "services": _FAKE_SERVICES,
    "production_models": [{"name": "JPCP", "production_version": "17", "staging_version": "18"}],
}
FAKE_APPROVALS = [
    {
        "model_id": "JPCP",
        "status": "pending",
        "requested_at": "2026-05-21T10:00:00",
        "commit_sha": "abc1234",
        "commit_msg": "fix",
        "changed_files": [],
        "id": "u1",
        "prefect_run_id": None,
        "reject_reason": None,
        "resolved_at": None,
    }
]


def test_status_shows_summary():
    with patch(
        "examlops.cli.commands.status._client.get", side_effect=[FAKE_STATUS, FAKE_APPROVALS]
    ):
        result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "ok" in result.output.lower() or "JPCP" in result.output


def test_status_json():
    with patch("examlops.cli.commands.status._client.get", return_value=FAKE_STATUS):
        result = runner.invoke(app, ["--json", "status"])
    assert '"control_plane"' in result.output
