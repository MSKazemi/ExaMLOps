"""What a second control-plane replica needs, and what /health says about it (plan P5.1).

Two things were process-local and are shared now: the ModelZoo runtime settings (a PUT reached
only the replica that received it) and the "retrain already in progress" check, which ran outside
the insert's lock (tested in test_v1_commands.py). ``horizontal_scaling_safe`` used to be a
hard-coded ``False`` with two blockers that were not blockers. It is now computed from the
configuration that actually decides the answer.
"""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

# Built at runtime: a literal credential-shaped string trips the platform secret scanner.
TOKEN = "-".join(("test", "token", "0123456789"))
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "ha.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_COMMAND_WORKERS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    cp_app._platform_schema_ready = False
    return cp_app


def _other_replica_writes(cp, key: str, value) -> None:
    """What a PUT through another replica leaves behind: a row in the shared store."""
    conn = cp._get_db()
    try:
        conn.execute(
            "INSERT INTO control_plane_settings (key, value, updated_at, updated_by) "
            "VALUES (?, ?, 'now', 'replica-2') ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"modelzoo.{key}", json.dumps(value)),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_settings_change_is_stored_shared_and_audited(cp):
    client = TestClient(cp.app)
    body = {"auto_retrain": True, "poll_interval_seconds": 120}
    answer = client.put("/v1/modelzoo/config", json=body, headers=HEADERS)
    assert answer.status_code == 200, answer.text
    assert answer.json()["auto_retrain"] is True and answer.json()["poll_interval_seconds"] == 120

    conn = cp._get_db()
    try:
        stored = dict(conn.execute("SELECT key, value FROM control_plane_settings").fetchall())
        audited = conn.execute(
            "SELECT actor, details FROM audit_events WHERE action='modelzoo_config_updated'"
        ).fetchall()
    finally:
        conn.close()
    assert stored == {"modelzoo.auto_retrain": "true", "modelzoo.poll_interval_seconds": "120"}
    assert len(audited) == 1 and json.loads(audited[0][1]) == body


def test_a_change_made_through_another_replica_reaches_this_one(cp, monkeypatch):
    monkeypatch.setattr(cp, "SETTINGS_TTL_SECONDS", 0.0)
    client = TestClient(cp.app)
    assert client.get("/v1/modelzoo/config", headers=HEADERS).json()["auto_retrain"] is False

    _other_replica_writes(cp, "auto_retrain", True)
    _other_replica_writes(cp, "poll_interval_seconds", 45)

    config = client.get("/v1/modelzoo/config", headers=HEADERS).json()
    assert config["auto_retrain"] is True and config["poll_interval_seconds"] == 45
    # And what the replica acts on, not only what it reports.
    assert cp._modelzoo_settings()["poll_interval_seconds"] == 45


def test_an_unreadable_store_keeps_the_last_settings_not_the_defaults(cp, monkeypatch):
    monkeypatch.setattr(cp, "SETTINGS_TTL_SECONDS", 0.0)
    _other_replica_writes(cp, "auto_retrain", True)
    assert cp._modelzoo_settings()["auto_retrain"] is True

    def broken():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(cp, "_get_db", broken)
    assert cp._modelzoo_settings()["auto_retrain"] is True  # not silently back to the default


def test_single_process_state_is_reported_as_the_blocker(cp):
    runtime = cp._runtime_capabilities()
    assert runtime["horizontal_scaling_safe"] is False
    assert "state_not_shared" in runtime["horizontal_scaling_blockers"]


def test_shared_state_and_a_real_broker_make_it_safe(cp, monkeypatch):
    monkeypatch.setattr(cp, "CONTROL_PLANE_STATE_BACKEND", "postgres")
    monkeypatch.setattr(cp, "EVENT_RELAY_SECONDS", 5.0)
    monkeypatch.setenv("EXAMLOPS_EVENT_PUBLISHER", "nats")
    monkeypatch.setenv("EXAMLOPS_COORDINATOR", "db")
    monkeypatch.setattr(cp, "_shared_outbox_stats", lambda: {"pending": 0})
    monkeypatch.setattr(cp, "_shared_outbox_oldest_age", lambda: None)
    runtime = cp._runtime_capabilities()
    assert runtime["horizontal_scaling_blockers"] == []
    assert runtime["horizontal_scaling_safe"] is True
    # Reported, but not blockers: a per-replica breaker is the usual design.
    assert "circuit_breaker_per_replica" in runtime["horizontal_scaling_notes"]


def test_a_log_publisher_is_still_a_blocker(cp, monkeypatch):
    monkeypatch.setattr(cp, "CONTROL_PLANE_STATE_BACKEND", "postgres")
    monkeypatch.setattr(cp, "EVENT_RELAY_SECONDS", 5.0)
    monkeypatch.setenv("EXAMLOPS_EVENT_PUBLISHER", "log")
    monkeypatch.setattr(cp, "_shared_outbox_stats", lambda: {"pending": 0})
    monkeypatch.setattr(cp, "_shared_outbox_oldest_age", lambda: None)
    runtime = cp._runtime_capabilities()
    assert runtime["horizontal_scaling_safe"] is False
    assert runtime["horizontal_scaling_blockers"] == ["event_publisher_process_local"]


def test_command_outcomes_are_exported_at_zero_before_any_command(cp):
    """The first dead command must be an increase from 0, or ControlPlaneCommandDead misses it:
    increase() over a series that first appears at 1 is 0."""
    body = TestClient(cp.app).get("/metrics").text
    for outcome in ("succeeded", "failed", "dead"):
        line = f'control_plane_command_outcomes_total{{kind="retrain",outcome="{outcome}"}}'
        assert line in body, outcome
