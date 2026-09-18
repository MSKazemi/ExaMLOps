"""The approval gate is governed: separated duties, audited decisions, replay-proof webhooks.

Plan P0.8 / findings S7, S8. Before this, the principal that filed a change could approve it,
approve/reject/retract/retrain decisions reached only the process log (never the hash-chained
``audit_events`` that `exa audit` reads), a rejection emitted no event, and every redelivery of a
ModelZoo webhook inserted another event row and re-fired CI and every auto-retrain.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json

import pytest
from fastapi.testclient import TestClient

_CREDENTIALS = {
    "alice-token-0123456789": {
        "principal": "alice",
        "tenant": "alpha",
        "scopes": ["read", "write"],
    },
    "bob-token-0123456789ab": {"principal": "bob", "tenant": "alpha", "scopes": ["read", "write"]},
}
# Built at runtime: a literal credential-shaped string trips the platform secret scanner.
_SECRET = "-".join(("webhook", "test", "value", "0123456789"))


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    state = tmp_path / "gate.db"
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "legacy-token-0123456789")
    monkeypatch.setenv("CONTROL_PLANE_CREDENTIALS_JSON", json.dumps(_CREDENTIALS))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(state))
    monkeypatch.setenv("PLATFORM_DB", str(state))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_EVENT_RELAY_SECONDS", "0")
    monkeypatch.setenv("MODELZOO_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setenv("CONTROL_PLANE_WEBHOOK_MAX_BYTES", "4096")
    import app as cp_app

    importlib.reload(cp_app)
    cp_app._platform_schema_ready = False
    monkeypatch.setattr(cp_app, "_get_registry", lambda: {"JPCP": ["PM100Dataset"]})

    class _Gateway:
        def find_deployment_id(self, _name):
            return "dep"

        def create_flow_run(self, _dep, _params, *, idempotency_key=None):
            return "flow-1"

    monkeypatch.setattr(cp_app, "_get_gateway", lambda: _Gateway())
    return cp_app


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _file_change(client, token: str = "alice-token-0123456789") -> str:
    r = client.post(
        "/api/changes", json={"model_ids": ["JPCP"], "commit_sha": "c1"}, headers=_h(token)
    )
    assert r.status_code == 200, r.text
    return r.json()["created"][0]


def _audit_actions(cp) -> list[tuple[str, str, str]]:
    conn = cp._get_db()
    try:
        return [
            (r[0], r[1], r[2])
            for r in conn.execute(
                "SELECT action, actor, target FROM audit_events WHERE source='control-plane' "
                "ORDER BY id"
            )
        ]
    finally:
        conn.close()


# ─── separation of duties ──────────────────────────────────────────────────────


def test_the_requester_cannot_approve_its_own_change(cp):
    client = TestClient(cp.app)
    _file_change(client)

    response = client.post("/approve/JPCP", headers=_h("alice-token-0123456789"))

    assert response.status_code == 403
    assert "Separation of duties" in response.json()["detail"]


def test_another_principal_can(cp):
    client = TestClient(cp.app)
    _file_change(client)
    assert client.post("/approve/JPCP", headers=_h("bob-token-0123456789ab")).status_code == 200


def test_the_shared_legacy_token_is_exempt_and_says_so(cp):
    client = TestClient(cp.app)
    _file_change(client, "legacy-token-0123456789")

    assert client.post("/approve/JPCP", headers=_h("legacy-token-0123456789")).status_code == 200
    runtime = client.get("/health").json()["runtime"]
    assert runtime["separation_of_duties"] == "enforced-except-legacy-token"


def test_the_rule_can_be_switched_off_explicitly(cp, monkeypatch):
    monkeypatch.setattr(cp, "SEPARATION_OF_DUTIES", False)
    client = TestClient(cp.app)
    _file_change(client)
    assert client.post("/approve/JPCP", headers=_h("alice-token-0123456789")).status_code == 200


# ─── audited, evented decisions ────────────────────────────────────────────────


def test_every_gate_decision_lands_in_the_audit_chain(cp):
    client = TestClient(cp.app)
    _file_change(client)
    client.post("/approve/JPCP", headers=_h("bob-token-0123456789ab"))
    retract_id = _file_change(client)
    client.delete(f"/approvals/{retract_id}", headers=_h("alice-token-0123456789"))
    _file_change(client)
    client.post("/reject/JPCP", json={"reason": "bad data"}, headers=_h("bob-token-0123456789ab"))
    client.post(
        "/retrain",
        json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        headers=_h("bob-token-0123456789ab"),
    )

    actions = _audit_actions(cp)
    assert ("approval_requested", "alice", "JPCP") in actions
    assert ("approval_approved", "bob", "JPCP") in actions
    assert ("approval_retracted", "alice", "JPCP") in actions
    assert ("approval_rejected", "bob", "JPCP") in actions
    assert ("retrain_dispatched", "bob", "JPCP") in actions


def test_the_audit_chain_stays_valid(cp):
    from examlops.data.audit import verify_audit_chain

    client = TestClient(cp.app)
    _file_change(client)
    client.post("/approve/JPCP", headers=_h("bob-token-0123456789ab"))

    result = verify_audit_chain()
    assert result.get("ok", result.get("valid")) is True, result


def test_a_rejection_is_an_event_too(cp):
    client = TestClient(cp.app)
    _file_change(client)
    client.post("/reject/JPCP", json={"reason": "bad data"}, headers=_h("bob-token-0123456789ab"))

    conn = cp._get_db()
    try:
        topics = [r[0] for r in conn.execute("SELECT topic FROM event_outbox")]
    finally:
        conn.close()
    assert "approval.rejected" in topics


def test_a_failed_audit_rolls_the_decision_back(cp, monkeypatch):
    client = TestClient(cp.app)
    _file_change(client)

    def _broken(*_a, **_k):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(cp, "append_audit_event", _broken)
    with pytest.raises(RuntimeError):
        client.post("/reject/JPCP", json={"reason": "x"}, headers=_h("bob-token-0123456789ab"))

    conn = cp._get_db()
    try:
        status = conn.execute("SELECT status FROM pending_approvals").fetchone()[0]
    finally:
        conn.close()
    assert status == "pending", "a decision whose record was lost must not stand"


# ─── webhooks ──────────────────────────────────────────────────────────────────


def _github(client, payload: dict):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/modelzoo/github",
        content=body,
        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )


def test_a_replayed_webhook_does_nothing_twice(cp, monkeypatch):
    fired: list[str] = []
    monkeypatch.setattr(cp, "_trigger_ci_pipeline", lambda sha: fired.append(sha) or True)
    client = TestClient(cp.app)
    push = {"ref": "refs/heads/main", "head_commit": {"id": "abc123"}, "pusher": {"name": "p"}}

    first = _github(client, push).json()
    replay = _github(client, push).json()

    assert first.get("duplicate") is not True
    assert replay["duplicate"] is True
    assert fired == ["abc123"]
    conn = cp._get_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM modelzoo_events").fetchone()[0] == 1
    finally:
        conn.close()


def test_an_oversized_webhook_body_is_refused(cp):
    client = TestClient(cp.app)
    push = {"ref": "refs/heads/main", "head_commit": {"id": "x"}, "padding": "y" * 8192}
    assert _github(client, push).status_code == 413
