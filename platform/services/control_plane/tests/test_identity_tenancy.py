"""Bearer-derived identity, scope, tenancy, and attribution tests."""

from __future__ import annotations

import importlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

_CREDENTIALS = {
    "alpha-writer-token": {
        "principal": "alice",
        "tenant": "alpha",
        "scopes": ["read", "write"],
    },
    "alpha-reader-token": {
        "principal": "auditor",
        "tenant": "alpha",
        "scopes": ["read"],
    },
    "alpha-other-token": {
        "principal": "bob",
        "tenant": "alpha",
        "scopes": ["read", "write"],
    },
    "beta-writer-token": {
        "principal": "brenda",
        "tenant": "beta",
        "scopes": ["read", "write"],
    },
    "write-only-token": {
        "principal": "builder",
        "tenant": "alpha",
        "scopes": ["write"],
    },
}


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    state_db = tmp_path / "identity.db"
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


class _Coordinator:
    def allow(self, _bucket, _limit, _window_s):
        return True

    def try_lock(self, _key, _holder, _ttl_s):
        return True

    def unlock(self, _key, _holder):
        return None


class _Gateway:
    def __init__(self) -> None:
        self.calls = 0

    def find_deployment_id(self, _name):
        return "deployment-1"

    def create_flow_run(self, _deployment_id, _parameters, *, idempotency_key=None):
        self.calls += 1
        return f"flow-{self.calls}"

    def get_flow_run(self, flow_run_id):
        return {"id": flow_run_id, "state": {"type": "RUNNING", "name": "Running"}}


@pytest.mark.parametrize(
    "credential_map",
    [
        "{not-json",
        json.dumps(
            {
                "legacy-control-token": {
                    "principal": "shadow",
                    "tenant": "other",
                    "scopes": ["read", "write"],
                }
            }
        ),
    ],
)
def test_invalid_credential_map_fails_closed_even_with_legacy_token(
    tmp_path, monkeypatch, credential_map
):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "legacy-control-token")
    monkeypatch.setenv("CONTROL_PLANE_CREDENTIALS_JSON", credential_map)
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "malformed.db"))
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    client = TestClient(cp_app.app)

    assert client.get("/approvals", headers=_auth("legacy-control-token")).status_code == 503
    assert (
        client.post(
            "/retrain",
            json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
            headers=_auth("legacy-control-token"),
        ).status_code
        == 503
    )


def test_scopes_protect_sensitive_reads_and_mutations(cp):
    client = TestClient(cp.app)

    assert client.get("/approvals", headers=_auth("write-only-token")).status_code == 403
    assert client.get("/approvals", headers=_auth("alpha-reader-token")).status_code == 200
    assert (
        client.post(
            "/api/changes",
            json={"model_ids": ["JPCP"]},
            headers=_auth("alpha-reader-token"),
        ).status_code
        == 403
    )


def test_tenant_cannot_list_or_resolve_another_tenants_approval(cp, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_coordinator", lambda: _Coordinator())
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)

    created = client.post(
        "/api/changes",
        json={"model_ids": ["JPCP"], "commit_sha": "abc"},
        headers=_auth("alpha-writer-token"),
    )
    assert created.status_code == 200
    assert client.get("/approvals", headers=_auth("beta-writer-token")).json() == []
    assert client.post("/approve/JPCP", headers=_auth("beta-writer-token")).status_code == 404
    assert gateway.calls == 0

    approved = client.post("/approve/JPCP", headers=_auth("alpha-writer-token"))
    assert approved.status_code == 200
    conn = cp._get_db()
    try:
        approval = conn.execute(
            "SELECT tenant, requested_by, resolved_by, status FROM pending_approvals"
        ).fetchone()
        command = conn.execute(
            "SELECT actor, tenant FROM control_plane_commands WHERE kind='approval'"
        ).fetchone()
        event = conn.execute(
            "SELECT actor, tenant, payload FROM event_outbox WHERE topic='approval.approved'"
        ).fetchone()
    finally:
        conn.close()
    assert tuple(approval) == ("alpha", "alice", "alice", "approved")
    assert tuple(command) == ("alice", "alpha")
    assert tuple(event[:2]) == ("alice", "alpha")
    assert json.loads(event[2])["tenant"] == "alpha"


def test_request_parameters_cannot_spoof_command_or_event_identity(cp, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_coordinator", lambda: _Coordinator())
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    response = TestClient(cp.app).post(
        "/retrain",
        json={
            "model_name": "JPCP",
            "dataset_name": "PM100Dataset",
            "parameters": {"actor": "mallory", "tenant": "beta"},
        },
        headers={**_auth("alpha-writer-token"), "X-Idempotency-Key": "spoof-attempt"},
    )
    assert response.status_code == 200

    conn = cp._get_db()
    try:
        command = conn.execute(
            "SELECT actor, tenant FROM control_plane_commands WHERE kind='retrain'"
        ).fetchone()
        event = conn.execute(
            "SELECT actor, tenant, payload FROM event_outbox WHERE topic='retrain.scheduled'"
        ).fetchone()
    finally:
        conn.close()
    assert tuple(command) == ("alice", "alpha")
    assert tuple(event[:2]) == ("alice", "alpha")
    assert json.loads(event[2])["actor"] == "alice"
    assert json.loads(event[2])["tenant"] == "alpha"


def test_idempotency_keys_are_namespaced_by_verified_principal(cp, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_coordinator", lambda: _Coordinator())
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)
    body = {"model_name": "JPCP", "dataset_name": "PM100Dataset"}

    assert (
        client.post(
            "/retrain", json=body, headers={**_auth("alpha-writer-token"), "X-Idempotency-Key": "7"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/retrain", json=body, headers={**_auth("alpha-other-token"), "X-Idempotency-Key": "7"}
        ).status_code
        == 200
    )
    assert gateway.calls == 2


def test_flow_status_does_not_cross_verified_tenant_boundary(cp, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_coordinator", lambda: _Coordinator())
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)
    scheduled = client.post(
        "/retrain",
        json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        headers=_auth("alpha-writer-token"),
    )
    assert scheduled.status_code == 200
    flow_run_id = scheduled.json()["flow_run_id"]

    assert (
        client.get(f"/retrain/{flow_run_id}", headers=_auth("beta-writer-token")).status_code == 404
    )
    assert (
        client.get(f"/retrain/{flow_run_id}", headers=_auth("alpha-reader-token")).status_code
        == 200
    )


def test_legacy_token_keeps_default_tenant_full_access(cp, monkeypatch):
    monkeypatch.setattr(cp, "_get_coordinator", lambda: _Coordinator())
    response = TestClient(cp.app).post(
        "/api/changes",
        json={"model_ids": ["JPCP"]},
        headers=_auth("legacy-control-token"),
    )
    assert response.status_code == 200
    conn = cp._get_db()
    try:
        row = conn.execute("SELECT tenant, requested_by FROM pending_approvals").fetchone()
    finally:
        conn.close()
    assert tuple(row) == ("default", "legacy")


def test_existing_sqlite_tables_gain_identity_columns(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE pending_approvals (id TEXT PRIMARY KEY, model_id TEXT, status TEXT);
        CREATE TABLE control_plane_commands (
            command_key TEXT PRIMARY KEY, state TEXT, updated_at TEXT
        );
        CREATE TABLE event_outbox (id INTEGER PRIMARY KEY, published_at DATETIME);
        """
    )
    conn.close()
    monkeypatch.setenv("CONTROL_PLANE_DB", str(path))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    import app as cp_app

    importlib.reload(cp_app)
    migrated = cp_app._get_db()
    try:
        assert {r[1] for r in migrated.execute("PRAGMA table_info(pending_approvals)")} >= {
            "tenant",
            "requested_by",
            "resolved_by",
        }
        assert {r[1] for r in migrated.execute("PRAGMA table_info(control_plane_commands)")} >= {
            "actor",
            "tenant",
        }
        assert {r[1] for r in migrated.execute("PRAGMA table_info(event_outbox)")} >= {
            "actor",
            "tenant",
        }
    finally:
        migrated.close()


# ─── retraction: `exa approvals delete` finally has a route (plan P0.3 / finding B3) ──────────


def _pending(cp, client, token: str) -> str:
    created = client.post(
        "/api/changes", json={"model_ids": ["JPCP"]}, headers={"Authorization": f"Bearer {token}"}
    )
    assert created.status_code == 200, created.text
    return created.json()["created"][0]


def test_retraction_keeps_the_record_and_emits_an_event(cp):
    client = TestClient(cp.app)
    approval_id = _pending(cp, client, "alpha-writer-token")

    response = client.delete(
        f"/approvals/{approval_id}", headers={"Authorization": "Bearer alpha-writer-token"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "retracted"
    conn = cp._get_db()
    try:
        row = conn.execute(
            "SELECT status, resolved_by FROM pending_approvals WHERE id=?", (approval_id,)
        ).fetchone()
        topics = [r[0] for r in conn.execute("SELECT topic FROM event_outbox")]
    finally:
        conn.close()
    assert row is not None, "a retraction must never erase the governance record"
    assert row[0] == "retracted"
    assert row[1]
    assert "approval.retracted" in topics


def test_retraction_is_tenant_scoped(cp):
    client = TestClient(cp.app)
    approval_id = _pending(cp, client, "alpha-writer-token")

    response = client.delete(
        f"/approvals/{approval_id}", headers={"Authorization": "Bearer beta-writer-token"}
    )

    assert response.status_code == 404


def test_only_pending_approvals_can_be_retracted(cp):
    client = TestClient(cp.app)
    approval_id = _pending(cp, client, "alpha-writer-token")
    headers = {"Authorization": "Bearer alpha-writer-token"}
    assert client.delete(f"/approvals/{approval_id}", headers=headers).status_code == 200

    assert client.delete(f"/approvals/{approval_id}", headers=headers).status_code == 409


def test_retraction_needs_the_write_scope(cp):
    client = TestClient(cp.app)
    approval_id = _pending(cp, client, "alpha-writer-token")

    response = client.delete(
        f"/approvals/{approval_id}", headers={"Authorization": "Bearer alpha-reader-token"}
    )

    assert response.status_code == 403
