"""The model read endpoints require the same read scope as /status (audit finding D2).

`GET /models`, `/models/{name}/meta`, `/models/{name}/readme` and
`/models/{name}/images/{filename}` sat unauthenticated next to a `/status` that demanded a
read-scoped bearer — the registry and per-model metadata were readable by anyone who could
reach the port. `/health` and `/ready` stay open on purpose: probes carry no credential.
"""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

_CREDENTIALS = {
    "reader-token": {"principal": "auditor", "tenant": "alpha", "scopes": ["read"]},
    "write-only-token": {"principal": "builder", "tenant": "alpha", "scopes": ["write"]},
}


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    state_db = tmp_path / "models-auth.db"
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "legacy-control-token")
    monkeypatch.setenv("CONTROL_PLANE_CREDENTIALS_JSON", json.dumps(_CREDENTIALS))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(state_db))
    monkeypatch.setenv("PLATFORM_DB", str(state_db))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_EVENT_RELAY_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    monkeypatch.setattr(cp_app, "_get_registry", lambda: {"JPCP": ["PM100Dataset"]})
    return cp_app


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_MODEL_PATHS = (
    "/models",
    "/models/JPCP/meta",
    "/models/JPCP/readme",
    "/models/JPCP/images/plot.png",
)


@pytest.mark.parametrize("path", _MODEL_PATHS)
def test_model_endpoints_reject_missing_token(cp, path):
    client = TestClient(cp.app)
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", _MODEL_PATHS)
def test_model_endpoints_reject_invalid_token(cp, path):
    client = TestClient(cp.app)
    assert client.get(path, headers=_auth("not-a-real-token")).status_code == 403


@pytest.mark.parametrize("path", _MODEL_PATHS)
def test_model_endpoints_reject_write_only_scope(cp, path):
    client = TestClient(cp.app)
    assert client.get(path, headers=_auth("write-only-token")).status_code == 403


def test_models_list_readable_with_read_scope(cp):
    client = TestClient(cp.app)
    r = client.get("/models", headers=_auth("reader-token"))
    assert r.status_code == 200
    assert r.json() == [{"model_name": "JPCP", "datasets": ["PM100Dataset"]}]


def test_health_and_ready_stay_open(cp):
    client = TestClient(cp.app)
    assert client.get("/ready").status_code == 200
    assert client.get("/health").status_code == 200
