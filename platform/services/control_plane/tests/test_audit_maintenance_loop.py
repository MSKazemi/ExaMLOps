"""The control plane runs the ADR 0028 audit maintenance on a timer, and a failure is visible.

``_audit_maintenance_once`` is what the ``audit-maintenance`` thread runs every
``EXAMLOPS_AUDIT_MAINTENANCE_SECONDS``. A degraded cycle must move
``examlops_audit_maintenance_errors_total`` (what ``AuditMaintenanceFailing`` reads), a crashing
cycle must be counted rather than end the loop, and only a clean cycle moves the heartbeat.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

TOKEN = "-".join(("test", "token", "0123456789"))


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_COMMAND_WORKERS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    return cp_app


def _metric(cp, name: str) -> float:
    body = TestClient(cp.app).get("/metrics").text
    return sum(
        float(line.rsplit(" ", 1)[1])
        for line in body.splitlines()
        if line.startswith(name) and not line.startswith("#")
    )


def test_a_degraded_cycle_is_counted_per_failed_step(cp, monkeypatch):
    from examlops import audit_maintenance

    before = _metric(cp, "examlops_audit_maintenance_errors_total")
    monkeypatch.setattr(
        audit_maintenance,
        "run_cycle",
        lambda: {"status": "degraded", "failed_steps": ["checkpoint", "prune"]},
    )
    assert cp._audit_maintenance_once()["status"] == "degraded"
    assert _metric(cp, "examlops_audit_maintenance_errors_total") == before + 2


def test_a_crashing_cycle_is_counted_and_returned_not_raised(cp, monkeypatch):
    from examlops import audit_maintenance

    def boom():
        raise RuntimeError("platform store unavailable")

    before = _metric(cp, "examlops_audit_maintenance_errors_total")
    monkeypatch.setattr(audit_maintenance, "run_cycle", boom)
    res = cp._audit_maintenance_once()
    assert res["status"] == "error" and "unavailable" in res["reason"]
    assert _metric(cp, "examlops_audit_maintenance_errors_total") == before + 1


def test_only_a_clean_cycle_moves_the_heartbeat(cp, monkeypatch):
    from examlops import audit_maintenance

    monkeypatch.setattr(audit_maintenance, "run_cycle", lambda: {"status": "skipped"})
    cp._metrics.audit_maintenance_last_success.set(0)
    cp._audit_maintenance_once()
    assert _metric(cp, "examlops_audit_maintenance_last_success_timestamp_seconds") == 0
    monkeypatch.setattr(audit_maintenance, "run_cycle", lambda: {"status": "ok"})
    cp._audit_maintenance_once()
    assert _metric(cp, "examlops_audit_maintenance_last_success_timestamp_seconds") > 0


def test_the_schedule_can_be_turned_off(cp, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUDIT_MAINTENANCE_SECONDS", "0")
    before = len(cp._background_threads)
    cp._start_audit_maintenance()
    assert len(cp._background_threads) == before


def test_the_schedule_starts_a_named_thread(cp, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUDIT_MAINTENANCE_SECONDS", "3600")
    cp._start_audit_maintenance()
    try:
        names = [t.name for t in cp._background_threads]
        assert "audit-maintenance" in names
    finally:
        cp._stop_event.set()
        for t in list(cp._background_threads):
            t.join(timeout=5)
        cp._background_threads.clear()
        cp._stop_event.clear()
