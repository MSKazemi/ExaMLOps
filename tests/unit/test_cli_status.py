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
# The live `/status` carries exactly `services` and `pending_approvals` — see
# `test_control_plane_status_contract.py`. This fixture used to add a `production_models` key that
# no version of the endpoint has ever sent, which is why the table it fed was green for a feature
# that did not work.
FAKE_STATUS = {
    "status": "ok",
    "pending_approvals": 2,
    "auth_configured": True,
    "services": _FAKE_SERVICES,
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
        "examlops.cli.commands.status._client.get",
        side_effect=[FAKE_STATUS, _REGISTRY, FAKE_APPROVALS],
    ):
        result = runner.invoke(app, ["status"], terminal_width=200)
    assert result.exit_code == 0
    # `"ok" in output.lower()` was the old assertion, and every green cell contains it — so it
    # could not distinguish a working command from a broken one. Name the things the command is
    # actually for.
    out = result.output
    assert "Service Health" in out
    assert "2 pending approval" in out
    assert "JPCP" in out and "17" in out, "the production model table is part of the summary"


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


# ── production models: three outcomes, and the command could express only one ────────────────
#
# `exa status --help` promises "service health, pending approvals, production models", and the
# section read a `production_models` key out of `/status`. That endpoint has never sent one
# (`test_control_plane_status_contract.py` pins it), so the list was empty on every real call — and
# the renderer printed *nothing at all* for an empty list. The screen therefore said, by silence,
# that no model was in production, whether the platform had three or the registry was unreadable.

_REGISTRY = {
    "registered_models": [
        {
            "name": "JPCP",
            "aliases": [
                {"alias": "Production", "version": "17"},
                {"alias": "Staging", "version": "18"},
            ],
        },
        {"name": "unaliased", "aliases": []},
    ]
}


def _run(status_payload, registry):
    """Invoke `exa status`, routing the /status and registry-search calls separately."""

    def get(url, *a, **k):
        if "registered-models/search" in url:
            if isinstance(registry, Exception):
                raise registry
            return registry
        return status_payload

    with patch("examlops.cli.commands.status._client.get", get):
        result = runner.invoke(app, ["status"], terminal_width=200)
    assert result.exit_code == 0, result.output
    return result.output


_NO_APPROVALS = {**FAKE_STATUS, "pending_approvals": 0, "services": _PROBED}


def test_production_models_are_listed():
    out = _run(_NO_APPROVALS, _REGISTRY)
    assert "Production Models" in out
    assert "JPCP" in out and "17" in out and "18" in out
    assert "unaliased" not in out, "a model with no lifecycle alias is not in production"


def test_no_production_models_says_so_instead_of_printing_nothing():
    out = _run(_NO_APPROVALS, {"registered_models": []})
    assert "No model carries a Production or Staging alias" in out


def test_an_unreadable_registry_is_reported_as_unknown():
    from examlops.cli import _client

    out = _run(_NO_APPROVALS, _client.ClientError("registry refused"))
    assert "production models unknown" in out
    assert "No model carries" not in out, "unknown must not be rendered as measured-and-none"


def test_json_carries_the_production_models_the_table_shows():
    import json

    def get(url, *a, **k):
        return _REGISTRY if "registered-models/search" in url else _NO_APPROVALS

    with patch("examlops.cli.commands.status._client.get", get):
        result = runner.invoke(app, ["--json", "status"])
    body = json.loads(result.stdout)
    assert [m["name"] for m in body["production_models"]] == ["JPCP"], (
        "`--json` emitted the raw control-plane payload, so it disagreed with the table"
    )


# ── a service the control plane never mentioned is not a service that is down ────────────────


def test_an_unreported_service_is_not_called_unreachable():
    """An older control plane omits a key; `svc.get("ok", False)` read that as a failed probe."""
    partial = {k: v for k, v in _PROBED.items() if k != "dashboard"}
    out = _run({**_NO_APPROVALS, "services": partial}, {"registered_models": []})
    assert "not reported" in out
    assert "1 service not reported by the control plane" in out
    assert "unreachable" not in out, "nothing was probed, so nothing may be called unreachable"
