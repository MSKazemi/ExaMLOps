"""Unit tests for the Phase 4 control-plane FastAPI service.

The tests use FastAPI's TestClient and stub the Prefect gateway and the
``_load_registry`` helper so no network or live registry import is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (
    str(REPO_ROOT),
    str(REPO_ROOT / "modelzoo"),
    str(REPO_ROOT / "platform/services/control_plane"),
):
    if p not in sys.path:
        sys.path.insert(0, p)

import app as cp  # noqa: E402

FAKE_REGISTRY = {
    "JPCP": ["PM100Dataset", "FDataDataset"],
    "MACK": ["FDataDataset"],
}


@pytest.fixture(autouse=True)
def _dispatch_probe_refused(monkeypatch):
    """The lifespan probes the dispatch deployment (plan P0.2). Every test here points it at a port
    that refuses instantly, so no test can reach a Prefect running on this machine."""
    monkeypatch.setattr(cp, "PREFECT_API_URL", "http://127.0.0.1:1/api")
    monkeypatch.setattr(cp, "_dispatch_cache", None)
    # This module imports `app` once; other suites in the same process may leave a warm registry
    # cache behind, which would hide FAKE_REGISTRY for up to its 60 s TTL.
    cp._invalidate_registry_cache()


@pytest.fixture
def client(monkeypatch, tmp_path):
    """TestClient with a fixed token, fake registry, and mocked Prefect gateway."""
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "control-plane.db"))
    monkeypatch.setattr(cp, "CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setattr(cp, "CONTROL_PLANE_DB", str(tmp_path / "control-plane.db"))
    monkeypatch.setattr(cp, "_load_registry", lambda: FAKE_REGISTRY)

    fake_gateway = MagicMock()
    fake_gateway.find_deployment_id.return_value = "dep-123"
    fake_gateway.create_flow_run.return_value = "run-abc"
    fake_gateway.get_flow_run.return_value = {
        "id": "run-abc",
        "state": {"type": "RUNNING", "name": "Running"},
    }
    monkeypatch.setattr(cp, "_get_gateway", lambda: fake_gateway)
    cp._gateway = fake_gateway

    with TestClient(cp.app) as c:
        c.gateway = fake_gateway  # type: ignore[attr-defined]
        yield c


# ── /health and /models ──────────────────────────────────────────────────────


def test_health_reports_registry_and_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["auth_configured"] is True
    assert "JPCP" in body["models"]


def test_models_endpoint_lists_known_models(client):
    # /models requires read scope since the D2 hardening (it exposes per-model Dataplane bus
    # UUIDs and promotion config — registry enumeration must not be anonymous).
    r = client.get("/models", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
    body = r.json()
    names = {entry["model_name"] for entry in body}
    assert names == {"JPCP", "MACK"}
    jpcp = next(e for e in body if e["model_name"] == "JPCP")
    assert sorted(jpcp["datasets"]) == ["FDataDataset", "PM100Dataset"]


# ── POST /retrain auth ───────────────────────────────────────────────────────


class TestRetrainAuth:
    def test_missing_token_is_401(self, client):
        r = client.post(
            "/retrain",
            json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        )
        assert r.status_code == 401

    def test_wrong_token_is_403(self, client):
        r = client.post(
            "/retrain",
            json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
            headers={"Authorization": "Bearer nope"},
        )
        assert r.status_code == 403

    def test_unset_token_is_503(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "control-plane.db"))
        monkeypatch.setattr(cp, "CONTROL_PLANE_TOKEN", "")
        monkeypatch.setattr(cp, "CONTROL_PLANE_DB", str(tmp_path / "control-plane.db"))
        monkeypatch.setattr(cp, "_load_registry", lambda: FAKE_REGISTRY)
        with TestClient(cp.app) as c:
            r = c.post(
                "/retrain",
                json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
                headers={"Authorization": "Bearer test-token"},
            )
        assert r.status_code == 503


# ── POST /retrain validation + success path ──────────────────────────────────


class TestRetrainValidation:
    def _auth(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-token"}

    def test_unknown_model_is_400(self, client):
        r = client.post(
            "/retrain",
            json={"model_name": "DoesNotExist", "dataset_name": "PM100Dataset"},
            headers=self._auth(),
        )
        assert r.status_code == 400
        assert "Unknown model" in r.json()["detail"]

    def test_unsupported_dataset_is_400(self, client):
        r = client.post(
            "/retrain",
            json={"model_name": "MACK", "dataset_name": "PM100Dataset"},  # MACK only supports FData
            headers=self._auth(),
        )
        assert r.status_code == 400
        assert "not supported by" in r.json()["detail"]

    def test_happy_path_creates_flow_run(self, client):
        r = client.post(
            "/retrain",
            json={
                "model_name": "JPCP",
                "dataset_name": "PM100Dataset",
                "is_dummy": True,
                "backend_name": "minio",
                "parameters": {"reason": "smoke"},
            },
            headers=self._auth(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["flow_run_id"] == "run-abc"
        assert body["status_url"] == "/retrain/run-abc"
        assert body["parameters"]["model_name"] == "JPCP"
        assert body["parameters"]["dataset_cls_name"] == "PM100Dataset"
        assert body["parameters"]["backend_name"] == "minio"
        assert body["parameters"]["is_dummy"] is True
        assert body["parameters"]["reason"] == "smoke"

        client.gateway.find_deployment_id.assert_called_once()
        client.gateway.create_flow_run.assert_called_once()
        params_arg = client.gateway.create_flow_run.call_args.args[1]
        assert params_arg["model_name"] == "JPCP"
        assert params_arg["dataset_cls_name"] == "PM100Dataset"


# ── GET /retrain/{flow_run_id} ───────────────────────────────────────────────


class TestRetrainStatus:
    def test_running_state(self, client):
        r = client.get("/retrain/run-abc", headers={"Authorization": "Bearer test-token"})
        assert r.status_code == 200
        body = r.json()
        assert body["state_type"] == "RUNNING"
        assert body["state_name"] == "Running"
        assert body["is_terminal"] is False

    def test_completed_state_is_terminal(self, client, monkeypatch):
        client.gateway.get_flow_run.return_value = {
            "id": "run-z",
            "state": {"type": "COMPLETED", "name": "Completed"},
        }
        r = client.get("/retrain/run-z", headers={"Authorization": "Bearer test-token"})
        assert r.status_code == 200
        assert r.json()["is_terminal"] is True

    def test_failed_state_is_terminal(self, client):
        client.gateway.get_flow_run.return_value = {
            "id": "run-f",
            "state": {"type": "FAILED", "name": "Failed"},
        }
        r = client.get("/retrain/run-f", headers={"Authorization": "Bearer test-token"})
        assert r.json()["is_terminal"] is True


# ── PrefectGateway URL building ──────────────────────────────────────────────


class TestPrefectGatewaySlugParsing:
    def test_rejects_unsplittable_deployment_name(self):
        gw = cp.PrefectGateway(api_url="http://x")
        with pytest.raises(cp.HTTPException) as exc:
            gw.find_deployment_id("nope")
        assert exc.value.status_code == 400

    def test_constructs_correct_lookup_url(self, monkeypatch):
        gw = cp.PrefectGateway(api_url="http://prefect/api")
        captured: dict[str, str] = {}

        def fake_get(url):
            captured["url"] = url
            return {"id": "abc"}

        monkeypatch.setattr(gw, "_get", fake_get)
        result = gw.find_deployment_id("flowA/depB")
        assert result == "abc"
        assert captured["url"] == "http://prefect/api/deployments/name/flowA/depB"


def test_models_endpoint_requires_token(client):
    """D2 regression guard: the registry (incl. Dataplane bus UUIDs) is not anonymous."""
    assert client.get("/models").status_code == 401
