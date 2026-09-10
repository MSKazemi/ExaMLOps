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


# ─── Admission must never wedge a tenant (plan P0.1 / finding B1) ─────────────────────────────
#
# The HTTP caller is the worker for this synchronous API, so a request refused at capacity has
# already gone away with its answer. Its admission row used to stay ``queued`` at the head of the
# tenant's FIFO, and admission then refused every *other* key because it was not the head. Every
# internal caller uses a fresh key per request, so nothing ever retried the head: one refusal
# stopped every retrain in the tenant, permanently. Reproduced 2026-09-10 before this fix.


def _params(n: int) -> dict[str, object]:
    return {"model_name": "JPCP", "dataset_cls_name": "PM100Dataset", "n": n}


def _admission_states(cp) -> list[str]:
    conn = cp._get_db()
    try:
        return [r[0] for r in conn.execute("SELECT state FROM admission_queue ORDER BY id")]
    finally:
        conn.close()


def test_refused_admission_does_not_wedge_the_tenant(cp, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ADMISSION_PER_TENANT", "2")
    first = cp._claim_command("k1", "retrain", _params(1))
    second = cp._claim_command("k2", "retrain", _params(2))
    refused = cp._claim_command("k3", "retrain", _params(3))
    assert (first.outcome, second.outcome, refused.outcome) == ("claimed", "claimed", "capacity")

    for key, claim in (("k1", first), ("k2", second)):
        cp._complete_command(
            key, {"flow_run_id": key}, event_topic="t", event_payload={}, attempt=claim.attempt
        )

    # Capacity is free again: a brand-new request is admitted, not blocked behind k3's ghost row.
    assert cp._claim_command("k4", "retrain", _params(4)).outcome == "claimed"
    assert "queued" not in _admission_states(cp)


def test_refused_key_can_be_retried_after_capacity_frees(cp, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ADMISSION_PER_TENANT", "1")
    first = cp._claim_command("k1", "retrain", _params(1))
    assert cp._claim_command("k2", "retrain", _params(2)).outcome == "capacity"
    cp._complete_command(
        "k1", {"flow_run_id": "r1"}, event_topic="t", event_payload={}, attempt=first.attempt
    )
    # The caller that was told to retry does so with the same idempotency key.
    assert cp._claim_command("k2", "retrain", _params(2)).outcome == "claimed"


def test_crashed_dispatch_releases_its_admission_slot(cp, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ADMISSION_PER_TENANT", "1")
    assert cp._claim_command("k1", "retrain", _params(1)).outcome == "claimed"
    # The worker dies mid-dispatch: the command lease goes stale and nobody completes it.
    conn = cp._get_db()
    try:
        conn.execute(
            "UPDATE control_plane_commands SET updated_at='2020-01-01T00:00:00' "
            "WHERE command_key='k1'"
        )
        conn.commit()
    finally:
        conn.close()

    assert cp._claim_command("k2", "retrain", _params(2)).outcome == "claimed"


def test_retrain_at_capacity_is_429_with_retry_after(cp, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ADMISSION_PER_TENANT", "1")
    assert cp._claim_command("held", "retrain", _params(0)).outcome == "claimed"
    monkeypatch.setattr(cp, "_get_gateway", lambda: _Gateway())

    response = TestClient(cp.app).post(
        "/retrain", json={"model_name": "JPCP", "dataset_name": "PM100Dataset"}, headers=_headers()
    )

    assert response.status_code == 429
    assert response.headers.get("Retry-After")
    assert "capacity" in response.json()["detail"]
