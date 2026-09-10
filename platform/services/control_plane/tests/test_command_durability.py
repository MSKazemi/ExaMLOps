"""Durable command, admission, and outbox behavior for Prefect dispatches."""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "commands.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    monkeypatch.setattr(cp_app, "_get_registry", lambda: {"JPCP": ["PM100Dataset", "Other"]})
    return cp_app


def _headers(key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer test-token"}
    if key:
        headers["X-Idempotency-Key"] = key
    return headers


class _Gateway:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def find_deployment_id(self, _name: str) -> str:
        return "deployment-1"

    def create_flow_run(self, _deployment_id, _parameters, *, idempotency_key=None) -> str:
        self.calls.append(idempotency_key)
        return "flow-1"


def test_retrain_replays_durable_result_and_commits_admission_and_outbox(cp, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)
    body = {"model_name": "JPCP", "dataset_name": "PM100Dataset"}

    first = client.post("/retrain", json=body, headers=_headers("request-7"))
    assert first.status_code == 200
    # The old RAM cache must not be what makes the retry idempotent.
    cp._idempotency_cache.clear()
    second = client.post("/retrain", json=body, headers=_headers("request-7"))

    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(gateway.calls) == 1
    assert gateway.calls[0].startswith("retrain:")

    conn = cp._get_db()
    try:
        command = conn.execute("SELECT state, attempts FROM control_plane_commands").fetchone()
        admission = conn.execute("SELECT state FROM admission_queue").fetchone()
        event = conn.execute("SELECT topic, payload FROM event_outbox").fetchone()
    finally:
        conn.close()
    assert tuple(command) == ("succeeded", 1)
    assert admission[0] == "done"
    assert event[0] == "retrain.scheduled"
    assert json.loads(event[1])["flow_run_id"] == "flow-1"


def test_idempotency_key_cannot_be_reused_for_different_input(cp, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)

    first = client.post(
        "/retrain",
        json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        headers=_headers("same-key"),
    )
    cp._idempotency_cache.clear()
    second = client.post(
        "/retrain",
        json={"model_name": "JPCP", "dataset_name": "Other"},
        headers=_headers("same-key"),
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(gateway.calls) == 1


def test_stale_approval_dispatch_is_recovered_with_same_prefect_key(cp, monkeypatch):
    conn = cp._get_db()
    try:
        conn.execute(
            "INSERT INTO pending_approvals (id, model_id, status, requested_at) "
            "VALUES ('approval-1', 'JPCP', 'approving', '2020-01-01T00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    parameters = {
        "model_name": "JPCP",
        "dataset_cls_name": "PM100Dataset",
        "is_dummy": False,
        "backend_name": None,
    }
    assert (
        cp._claim_command(
            "approval:approval-1",
            "approval",
            parameters,
            approval_id="approval-1",
            actor="legacy",
        ).outcome
        == "claimed"
    )
    conn = cp._get_db()
    try:
        conn.execute(
            "UPDATE control_plane_commands SET updated_at='2020-01-01T00:00:00' "
            "WHERE command_key='approval:approval-1'"
        )
        conn.commit()
    finally:
        conn.close()

    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    response = TestClient(cp.app).post("/approve/JPCP", headers=_headers())

    assert response.status_code == 200
    assert gateway.calls == ["approval:approval-1"]
    conn = cp._get_db()
    try:
        approval = conn.execute(
            "SELECT status, prefect_run_id FROM pending_approvals WHERE id='approval-1'"
        ).fetchone()
    finally:
        conn.close()
    assert tuple(approval) == ("approved", "flow-1")


def test_superseded_worker_cannot_complete_newer_claim(cp):
    parameters = {"model_name": "JPCP", "dataset_cls_name": "PM100Dataset"}
    first = cp._claim_command("retrain:fenced", "retrain", parameters)
    assert first.outcome == "claimed"

    conn = cp._get_db()
    try:
        conn.execute(
            "UPDATE control_plane_commands SET updated_at='2020-01-01T00:00:00' "
            "WHERE command_key='retrain:fenced'"
        )
        conn.commit()
    finally:
        conn.close()
    second = cp._claim_command("retrain:fenced", "retrain", parameters)
    assert second.outcome == "claimed"
    assert second.attempt == 2

    with pytest.raises(RuntimeError, match="lease was superseded"):
        cp._complete_command(
            "retrain:fenced",
            {"flow_run_id": "stale-flow"},
            event_topic="retrain.scheduled",
            event_payload={"flow_run_id": "stale-flow"},
            attempt=first.attempt or 0,
        )

    cp._complete_command(
        "retrain:fenced",
        {"flow_run_id": "winning-flow"},
        event_topic="retrain.scheduled",
        event_payload={"flow_run_id": "winning-flow"},
        attempt=second.attempt or 0,
    )
    conn = cp._get_db()
    try:
        command = conn.execute(
            "SELECT state, prefect_run_id FROM control_plane_commands "
            "WHERE command_key='retrain:fenced'"
        ).fetchone()
        events = conn.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE topic='retrain.scheduled'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert tuple(command) == ("succeeded", "winning-flow")
    assert events == 1


def test_prefect_create_flow_run_sends_idempotency_key(cp, monkeypatch):
    gateway = cp.PrefectGateway("http://prefect/api")
    captured = {}

    def fake_post(url, body):
        captured.update({"url": url, "body": body})
        return {"id": "flow-9"}

    monkeypatch.setattr(gateway, "_post", fake_post)
    assert gateway.create_flow_run("dep-9", {"x": 1}, idempotency_key="command-9") == "flow-9"
    assert captured["body"] == {"parameters": {"x": 1}, "idempotency_key": "command-9"}


def test_modelzoo_retry_does_not_create_a_second_flow(cp, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)

    cp._auto_retrain_model("JPCP", "PM100Dataset", "commit-1")
    cp._auto_retrain_model("JPCP", "PM100Dataset", "commit-1")

    assert gateway.calls == ["modelzoo:commit-1:JPCP:PM100Dataset"]
    conn = cp._get_db()
    try:
        freshness = conn.execute(
            "SELECT is_stale, last_retrain_commit FROM model_freshness WHERE model_id='JPCP'"
        ).fetchone()
        event_count = conn.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE topic='modelzoo.retrain_scheduled'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert tuple(freshness) == (0, "commit-1")
    assert event_count == 1
