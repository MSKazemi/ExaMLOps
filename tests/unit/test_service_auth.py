"""MLflow and Prefect can require authentication, and every platform caller sends it (plan P3.6).

Both servers were reachable by anyone on the network. They can now require credentials —
MLflow's basic-auth app, Prefect's API auth string — which their SDKs read from the environment on
their own; these tests pin the raw-HTTP callers that do not use the SDKs, and the compose wiring.
The end-to-end check (a real MLflow and Prefect refusing an anonymous call and accepting these
headers) was run against the images; see the plan tracker.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
import yaml

from examlops import service_auth

COMPOSE = Path(__file__).resolve().parents[2] / "platform/infra/docker-compose/docker-compose.yml"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (
        "MLFLOW_TRACKING_TOKEN",
        "MLFLOW_TRACKING_USERNAME",
        "MLFLOW_TRACKING_PASSWORD",
        "PREFECT_API_AUTH_STRING",
        "PREFECT_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _basic(value: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(value.encode()).decode()}


def test_nothing_is_sent_when_nothing_is_configured():
    assert service_auth.mlflow_headers() == {}
    assert service_auth.prefect_headers() == {}


def test_mlflow_uses_its_own_clients_variables(monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_USERNAME", "svc")
    monkeypatch.setenv("MLFLOW_TRACKING_PASSWORD", "pw")
    assert service_auth.mlflow_headers() == _basic("svc:pw")
    monkeypatch.setenv("MLFLOW_TRACKING_TOKEN", "tok")
    assert service_auth.mlflow_headers() == {"Authorization": "Bearer tok"}


def test_a_username_without_a_password_sends_nothing(monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_USERNAME", "svc")
    assert service_auth.mlflow_headers() == {}


def test_prefect_auth_string_and_key(monkeypatch):
    monkeypatch.setenv("PREFECT_API_KEY", "pnu_key")
    assert service_auth.prefect_headers() == {"Authorization": "Bearer pnu_key"}
    monkeypatch.setenv("PREFECT_API_AUTH_STRING", "svc:pw")
    assert service_auth.prefect_headers() == _basic("svc:pw")


def test_credentials_go_only_to_the_configured_servers(monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_USERNAME", "svc")
    monkeypatch.setenv("MLFLOW_TRACKING_PASSWORD", "pw")
    base = "http://mlflow:5000"

    assert service_auth.headers_for(f"{base}/api/2.0/mlflow/x", mlflow_base=base) == _basic(
        "svc:pw"
    )
    assert service_auth.headers_for(base, mlflow_base=base) == _basic("svc:pw")
    # A host that merely starts with the same characters is a different host.
    assert service_auth.headers_for("http://mlflow:50001/api", mlflow_base=base) == {}
    assert service_auth.headers_for("http://control-plane:8002/v1/x", mlflow_base=base) == {}


# ─── the callers ─────────────────────────────────────────────────────────────


def test_the_cli_client_authenticates_to_mlflow_and_not_elsewhere(monkeypatch):
    import urllib.request

    from examlops.cli import _client

    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://mlflow.test:5000")
    monkeypatch.setenv("MLFLOW_TRACKING_USERNAME", "svc")
    monkeypatch.setenv("MLFLOW_TRACKING_PASSWORD", "pw")

    to_mlflow = urllib.request.Request("http://mlflow.test:5000/api/2.0/mlflow/experiments/search")
    _client._platform_service_auth(to_mlflow, to_mlflow.full_url)
    assert to_mlflow.get_header("Authorization") == _basic("svc:pw")["Authorization"]

    elsewhere = urllib.request.Request("http://cp.test:8002/v1/models")
    _client._platform_service_auth(elsewhere, elsewhere.full_url)
    assert elsewhere.get_header("Authorization") is None

    already = urllib.request.Request(
        "http://mlflow.test:5000/api", headers={"Authorization": "Bearer mine"}
    )
    _client._platform_service_auth(already, already.full_url)
    assert already.get_header("Authorization") == "Bearer mine"


def test_the_snapshot_compiler_authenticates_to_mlflow(monkeypatch):
    import httpx

    from examlops import serving_snapshot

    monkeypatch.setenv("MLFLOW_TRACKING_USERNAME", "svc")
    monkeypatch.setenv("MLFLOW_TRACKING_PASSWORD", "pw")
    seen: dict = {}

    class _Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def get(self, *_a, **_k):
            raise ConnectionError("stop here")

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _Client)
    with pytest.raises(ConnectionError):
        serving_snapshot.compile_snapshot(mlflow_url="http://mlflow:5000")
    assert seen["headers"] == _basic("svc:pw")


# ─── compose ─────────────────────────────────────────────────────────────────


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def test_prefects_server_variable_is_never_set_empty():
    """Prefect reads PREFECT_SERVER_API_AUTH_STRING="" as auth-on with an empty password — which
    would refuse every client. Compose may only export it from a non-empty value."""
    for name, svc in _services().items():
        env = svc.get("environment") or {}
        assert "PREFECT_SERVER_API_AUTH_STRING" not in env, name
    script = "\n".join(_services()["orchestrator"]["command"])
    assert 'if [ -n "$${PREFECT_AUTH_STRING:-}" ]' in script
    assert "export PREFECT_SERVER_API_AUTH_STRING" in script


def test_mlflow_auth_is_opt_in_and_never_uses_the_bundled_admin_password():
    script = "\n".join(_services()["mlflow"]["command"])
    assert '"$${MLFLOW_AUTH:-}" = "basic"' in script
    assert "--app-name basic-auth" in script
    assert "password1234" not in script
    assert 'if [ -z "$${MLFLOW_ADMIN_PASSWORD:-}" ]' in script  # refuses to start without one
