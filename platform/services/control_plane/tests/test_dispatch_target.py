"""The dispatch-target contract: plan P0.2 / finding B2.

Until 2026-09-10 the control plane dispatched every retrain to `examlops_scheduled_training/nightly`,
which nothing created, and whose flow would have refused the parameters anyway. Every test here
used a gateway double, so the suite was green while every real dispatch 404'd. These tests pin the
contract at the one boundary the doubles hid: what Prefect says about the deployment.
"""

from __future__ import annotations

import importlib
import io
import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

_TRAINING_FLOW_SCHEMA = {
    "properties": {
        "model_name": {"type": "string"},
        "dataset_cls_name": {"type": "string"},
        "is_dummy": {"type": "boolean"},
        "backend_name": {"type": "string"},
    },
    "required": ["model_name", "dataset_cls_name"],
}
# The wrapper flow the old default named: it takes only `is_dummy`.
_SCHEDULED_WRAPPER_SCHEMA = {"properties": {"is_dummy": {"type": "boolean"}}, "required": []}


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "dispatch.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.delenv("PREFECT_DEPLOYMENT_NAME", raising=False)
    import app as cp_app

    importlib.reload(cp_app)
    monkeypatch.setattr(cp_app, "_get_registry", lambda: {"JPCP": ["PM100Dataset"]})
    return cp_app


def _prefect_answers(monkeypatch, *, schema=None, http_status=None):
    """Make the dispatch probe see one Prefect answer (a deployment document or an HTTP error)."""

    def _urlopen(req, timeout=None):  # noqa: ARG001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "/deployments/name/" not in url:
            raise urllib.error.URLError("refused")
        if http_status is not None:
            raise urllib.error.HTTPError(url, http_status, "err", {}, io.BytesIO(b""))
        body = json.dumps({"id": "dep-1", "parameter_openapi_schema": schema}).encode()
        return io.BytesIO(body)

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def test_default_target_is_the_deployment_the_platform_creates(cp):
    assert cp.PREFECT_DEPLOYMENT_NAME == "training_flow/examlops-dispatch"


def test_missing_target_degrades_status_but_keeps_the_api_in_rotation(cp, monkeypatch):
    _prefect_answers(monkeypatch, http_status=404)
    cp._run_startup_checks()
    client = TestClient(cp.app)

    health = client.get("/health").json()
    assert health["dispatch"]["state"] == "missing"
    assert "exa pipeline deploy" in health["dispatch"]["detail"]
    assert health["status"] == "degraded"
    # A stack whose training runner is not deployed yet still serves approvals and reads.
    assert health["ready"] is True
    assert client.get("/readyz").status_code == 200


def test_wrapper_flow_that_refuses_the_parameters_is_incompatible(cp, monkeypatch):
    _prefect_answers(monkeypatch, schema=_SCHEDULED_WRAPPER_SCHEMA)
    verdict = cp._dispatch_status(refresh=True)

    assert verdict["state"] == "incompatible"
    assert "does not accept" in verdict["detail"]
    assert "model_name" in verdict["detail"]


def test_flow_requiring_an_unsent_parameter_is_incompatible(cp, monkeypatch):
    schema = json.loads(json.dumps(_TRAINING_FLOW_SCHEMA))
    schema["properties"]["tenant_budget"] = {"type": "string"}
    schema["required"].append("tenant_budget")
    _prefect_answers(monkeypatch, schema=schema)

    verdict = cp._dispatch_status(refresh=True)

    assert verdict["state"] == "incompatible"
    assert "tenant_budget" in verdict["detail"]


def test_training_flow_target_is_ok(cp, monkeypatch):
    _prefect_answers(monkeypatch, schema=_TRAINING_FLOW_SCHEMA)
    verdict = cp._dispatch_status(refresh=True)

    assert verdict["state"] == "ok"
    assert set(verdict["parameters"]) == set(_TRAINING_FLOW_SCHEMA["properties"])


def test_unknown_parameter_is_rejected_before_any_durable_state(cp, monkeypatch):
    _prefect_answers(monkeypatch, schema=_TRAINING_FLOW_SCHEMA)
    cp._dispatch_status(refresh=True)

    response = TestClient(cp.app).post(
        "/retrain",
        json={
            "model_name": "JPCP",
            "dataset_name": "PM100Dataset",
            "parameters": {"learning_rate": 0.1},
        },
        headers=_headers(),
    )

    assert response.status_code == 400
    assert "learning_rate" in response.json()["detail"]
    conn = cp._get_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM control_plane_commands").fetchone()[0] == 0
    finally:
        conn.close()


def test_unreachable_prefect_does_not_invent_a_parameter_verdict(cp, monkeypatch):
    # The conftest points Prefect at a refusing port: no schema is known, so nothing is rejected
    # on its account — Prefect's own validation keeps the last word.
    verdict = cp._dispatch_status(refresh=True)
    assert verdict["state"] == "unreachable"
    assert verdict["parameters"] is None
    cp._check_dispatch_parameters({"anything": 1})


def test_missing_deployment_is_an_actionable_503_not_a_bare_404(cp, monkeypatch):
    _prefect_answers(monkeypatch, http_status=404)
    gateway = cp.PrefectGateway(api_url="http://prefect.invalid/api")

    with pytest.raises(cp.HTTPException) as caught:
        gateway.get_deployment("training_flow/examlops-dispatch")

    assert caught.value.status_code == 503
    assert "exa pipeline deploy" in caught.value.detail
