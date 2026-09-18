"""Each service holds exactly the action it performs (plan P3.2).

``write`` let its holder do every mutation, and every service held it — so the SeanerBUS bridge,
which only ever requests drift retrains, could also approve a model into training or reconfigure
the ModelZoo integration. Narrow scopes (``retrain``, ``approve``, ``changes``, ``admin``) give a
credential one action; ``write`` still implies them all, so existing credentials are unchanged.
Each service also gets its own principal, so the audit trail says which one acted.
"""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

CREDENTIALS = {
    "bridge-secret-token-value-01": {
        "principal": "seanerbus-bridge",
        "tenant": "default",
        "scopes": ["retrain"],
    },
    "ci-secret-token-value-00001": {"principal": "ci", "tenant": "default", "scopes": ["changes"]},
    "sysadmin-secret-token-0001": {
        "principal": "alice",
        "tenant": "default",
        "scopes": ["read", "approve"],
    },
    "platform-admin-token-0001": {"principal": "ops", "tenant": "default", "scopes": ["admin"]},
    "full-writer-token-000001": {"principal": "bob", "tenant": "default", "scopes": ["write"]},
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


BRIDGE, CI, SYSADMIN, ADMIN, WRITER = (_auth(t) for t in CREDENTIALS)


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_CREDENTIALS_JSON", json.dumps(CREDENTIALS))
    monkeypatch.delenv("CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    monkeypatch.setattr(cp_app, "_start_command_workers", lambda: None)
    return cp_app


def _retrain(client, headers) -> int:
    return client.post(
        "/v1/retrain",
        json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        headers=headers,
    ).status_code


def test_a_retrain_only_credential_can_request_a_retrain_and_nothing_else(cp):
    client = TestClient(cp.app)

    assert _retrain(client, BRIDGE) != 403
    assert client.post("/approve/JPCP", headers=BRIDGE).status_code == 403
    assert client.post("/admin/reload", headers=BRIDGE).status_code == 403
    assert client.put("/modelzoo/config", json={}, headers=BRIDGE).status_code == 403
    assert client.post("/api/changes", json={}, headers=BRIDGE).status_code == 403


def test_the_refusal_names_the_scopes_that_would_do(cp):
    detail = TestClient(cp.app).post("/admin/reload", headers=BRIDGE).json()["detail"]
    assert detail == "Missing 'write' or 'admin' scope"


def test_an_approver_can_decide_but_not_retrain(cp):
    client = TestClient(cp.app)
    assert client.post("/approve/JPCP", headers=SYSADMIN).status_code != 403
    assert _retrain(client, SYSADMIN) == 403


def test_ci_can_report_changes_only(cp):
    client = TestClient(cp.app)
    assert client.post("/api/changes", json={"changed_models": []}, headers=CI).status_code != 403
    assert client.post("/approve/JPCP", headers=CI).status_code == 403
    assert client.get("/approvals", headers=CI).status_code == 403  # no read either


def test_admin_scope_reaches_the_admin_routes_only(cp):
    client = TestClient(cp.app)
    assert client.post("/admin/reload", headers=ADMIN).status_code == 200
    assert _retrain(client, ADMIN) == 403


def test_write_still_implies_every_action(cp):
    client = TestClient(cp.app)
    assert _retrain(client, WRITER) != 403
    assert client.post("/admin/reload", headers=WRITER).status_code == 200


def test_the_command_records_which_service_asked(cp):
    client = TestClient(cp.app)
    command_id = client.post(
        "/v1/retrain",
        json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        headers=BRIDGE,
    ).json()["command_id"]
    conn = cp._get_db()
    try:
        actor = conn.execute(
            "SELECT actor FROM control_plane_commands WHERE command_key=?", (command_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert actor == "seanerbus-bridge"


def test_an_unknown_scope_is_a_configuration_error_not_a_silent_grant(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "CONTROL_PLANE_CREDENTIALS_JSON",
        json.dumps(
            {"some-token-value-00001": {"principal": "x", "tenant": "t", "scopes": ["root"]}}
        ),
    )
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "test.db"))
    import app as cp_app

    importlib.reload(cp_app)
    assert "scopes must be drawn from" in (cp_app._credential_config_error or "")
    response = TestClient(cp_app.app).get("/approvals", headers=_auth("some-token-value-00001"))
    assert response.status_code == 503
