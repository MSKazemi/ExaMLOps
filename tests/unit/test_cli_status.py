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


# ── the health table must not name an address that was never checked ─────────
#
# The control plane pings its in-network peers (`http://mlflow:5000` under compose). `exa status`
# printed the host port map (`:15000`) beside each verdict, so on any real deployment the reader
# was shown an address that had not been probed and — against a remote node — could not be opened
# from here either. The probed address now has its own column and comes from the payload.

_PROBED = {
    "control_plane": {"ok": True, "url": "self"},
    "mlflow": {"ok": True, "url": "http://mlflow:5000/health"},
    "prefect": {"ok": True, "active_runs": 0, "url": "http://orchestrator:4200/api/health"},
    "ray_serve": {"ok": True, "models": [], "url": "http://ray-serving:8001/models"},
    "dashboard": {"ok": True, "url": "http://dashboard:8099/api/health"},
}


def _status_output(services: dict) -> str:
    payload = {**FAKE_STATUS, "services": services, "pending_approvals": 0}
    with patch("examlops.cli.commands.status._client.get", return_value=payload):
        result = runner.invoke(app, ["status"], terminal_width=200)
    assert result.exit_code == 0
    return result.output


def test_checked_column_shows_the_address_that_was_probed():
    out = _status_output(_PROBED)
    assert "Checked" in out
    assert "mlflow:5000" in out
    assert "ray-serving:8001" in out


def test_host_port_map_is_not_presented_as_the_checked_address():
    """The other direction: when the payload reports no url, say so — never guess a port."""
    bare = {k: {kk: vv for kk, vv in v.items() if kk != "url"} for k, v in _PROBED.items()}
    out = _status_output(bare)
    for port in (":15000", ":14200", ":18001", ":18099"):
        assert port not in out, f"{port} was printed as if it had been checked"
