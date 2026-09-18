"""Skipper's MLflow / Prefect calls carry those servers' credentials when set (plan P3.6)."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

_CLI_SRC = Path(__file__).resolve().parents[3] / "platform" / "cli" / "src"
sys.path.insert(0, str(_CLI_SRC))

from skipper import config  # noqa: E402
from skipper.tools import _http  # noqa: E402


def test_mlflow_calls_are_authenticated_and_others_are_not(monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_USERNAME", "svc")
    monkeypatch.setenv("MLFLOW_TRACKING_PASSWORD", "pw")
    url = f"{config.MLFLOW_URL}/api/2.0/mlflow/registered-models/search"

    out = _http._with_platform_service_auth("mlflow", url, {})
    assert out["headers"]["Authorization"] == "Basic " + base64.b64encode(b"svc:pw").decode()
    # A different host, even when labelled "mlflow", gets nothing.
    assert _http._with_platform_service_auth("mlflow", "http://elsewhere:1/x", {}) == {}
    # The caller's own Authorization wins.
    mine = {"headers": {"Authorization": "Bearer mine"}}
    assert _http._with_platform_service_auth("mlflow", url, mine) == mine


def test_nothing_is_added_while_auth_is_off(monkeypatch):
    for var in ("MLFLOW_TRACKING_USERNAME", "MLFLOW_TRACKING_PASSWORD", "MLFLOW_TRACKING_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    url = f"{config.MLFLOW_URL}/api/2.0/mlflow/x"
    assert _http._with_platform_service_auth("mlflow", url, {}) == {}


def test_the_control_plane_bearer_comes_from_the_token_file(monkeypatch, tmp_path):
    """ADR 0125: the agent's workload identity (a JWT-SVID spiffe-helper rewrites) wins over the
    static token, and a rotation is used on the next call."""
    path = tmp_path / "control-plane.jwt"
    path.write_text("svid-agent\n")
    monkeypatch.setenv("CONTROL_PLANE_TOKEN_FILE", str(path))
    monkeypatch.setattr(config, "CONTROL_PLANE_TOKEN", "static-token")
    url = f"{config.CONTROL_PLANE_URL.rstrip('/')}/v1/commands"
    out = _http._with_control_plane_auth("control_plane", url, {})
    assert out["headers"]["Authorization"] == "Bearer svid-agent"
    path.write_text("svid-rotated\n")
    out = _http._with_control_plane_auth("control_plane", url, {})
    assert out["headers"]["Authorization"] == "Bearer svid-rotated"
