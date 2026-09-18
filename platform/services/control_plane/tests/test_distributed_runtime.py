"""Shared coordination, leader lease, and control-plane outbox relay tests."""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    state_db = tmp_path / "control-plane.db"
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(state_db))
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "wrong-platform.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_EVENT_RELAY_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    monkeypatch.setattr(cp_app, "_get_registry", lambda: {"JPCP": ["PM100Dataset"]})
    return cp_app


class _Coordinator:
    def __init__(self, *, lock_results: list[bool] | None = None, allowed: bool = True) -> None:
        self.lock_results = lock_results or [True]
        self.allowed = allowed
        self.allow_calls: list[tuple[Any, ...]] = []
        self.lock_calls: list[tuple[Any, ...]] = []
        self.unlock_calls: list[tuple[Any, ...]] = []

    def allow(self, bucket, limit, window_s):
        self.allow_calls.append((bucket, limit, window_s))
        return self.allowed

    def try_lock(self, key, holder, ttl_s):
        self.lock_calls.append((key, holder, ttl_s))
        return self.lock_results.pop(0)

    def unlock(self, key, holder):
        self.unlock_calls.append((key, holder))


class _Gateway:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def find_deployment_id(self, _name):
        return "deployment-1"

    def create_flow_run(self, deployment_id, parameters, *, idempotency_key=None):
        self.calls.append((deployment_id, parameters, idempotency_key))
        return "flow-1"


def test_retrain_uses_and_releases_shared_lock(cp, monkeypatch):
    coordinator = _Coordinator()
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_coordinator", lambda: coordinator)
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)

    response = TestClient(cp.app).post(
        "/retrain",
        json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        headers={"Authorization": "Bearer test-token", "X-Idempotency-Key": "shared-1"},
    )

    assert response.status_code == 200
    assert coordinator.allow_calls == [
        ("control-plane:writes:default", cp.RETRAIN_RATE_LIMIT_PER_MIN, 60.0)
    ]
    lock_key, holder, ttl = coordinator.lock_calls[0]
    assert lock_key == "control-plane:retrain:default:JPCP:PM100Dataset"
    assert ttl == cp.RETRAIN_LOCK_SECONDS
    assert coordinator.unlock_calls == [(lock_key, holder)]
    assert gateway.calls[0][2].startswith("retrain:")


def test_retrain_does_not_reach_prefect_without_shared_lock(cp, monkeypatch):
    coordinator = _Coordinator(lock_results=[False])
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_coordinator", lambda: coordinator)
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)

    response = TestClient(cp.app).post(
        "/retrain",
        json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 409
    assert gateway.calls == []


def test_modelzoo_poller_runs_only_while_lease_is_held(cp, monkeypatch):
    coordinator = _Coordinator(lock_results=[True, False])
    cycles = []
    monkeypatch.setattr(cp, "_get_coordinator", lambda: coordinator)
    monkeypatch.setattr(cp, "_run_poll_cycle", lambda: cycles.append("ran") or {"new": False})
    monkeypatch.setattr(cp, "_expire_old_approvals", lambda: 0)
    cp._modelzoo_config["poll_interval_seconds"] = 7

    assert cp._run_leased_poll_cycle("replica-1") is True
    assert cp._run_leased_poll_cycle("replica-1") is False

    assert cycles == ["ran"]
    lease_seconds = max(cp.POLLER_LEASE_SECONDS, 21)
    assert coordinator.lock_calls == [
        ("control-plane:modelzoo-poller", "replica-1", lease_seconds),
        ("control-plane:modelzoo-poller", "replica-1", lease_seconds),
    ]


def test_relay_reads_control_plane_sqlite_outbox_and_uses_selected_publisher(cp, monkeypatch):
    import examlops.events as events

    published = []

    class _Publisher:
        def publish(self, topic, payload, *, event_id):
            published.append((topic, payload, event_id))

    monkeypatch.setattr(events, "_publisher", _Publisher())
    conn = cp._get_db()
    try:
        cp.enqueue_event("control-plane.test", {"value": 7}, conn=conn)
        conn.commit()
    finally:
        conn.close()

    assert cp.os.environ["PLATFORM_DB"] == cp.CONTROL_PLANE_DB
    assert cp._relay_outbox_once() == {"claimed": 1, "published": 1, "failed": 0}
    assert published == [("control-plane.test", {"value": 7}, "outbox:1")]

    conn = cp._get_db()
    try:
        row = conn.execute("SELECT published_at, payload FROM event_outbox WHERE id=1").fetchone()
    finally:
        conn.close()
    assert row[0] is not None
    assert json.loads(row[1]) == {"value": 7}


def test_health_reports_shared_outbox_pending_published_and_poison_counts(cp):
    conn = cp._get_db()
    try:
        cp.enqueue_event("pending", {"state": "pending"}, conn=conn)
        cp.enqueue_event("published", {"state": "published"}, conn=conn)
        cp.enqueue_event("poison", {"state": "poison"}, conn=conn)
        conn.execute(
            "UPDATE event_outbox SET published_at = CURRENT_TIMESTAMP WHERE topic = 'published'"
        )
        conn.execute("UPDATE event_outbox SET attempts = 5 WHERE topic = 'poison'")
        conn.commit()
    finally:
        conn.close()

    runtime = TestClient(cp.app).get("/health").json()["runtime"]

    outbox = dict(runtime["outbox"])
    assert outbox.pop("oldest_pending_age_seconds") >= 0  # two are pending, so there is an age
    assert outbox == {"pending": 2, "published": 1, "poison": 1}
