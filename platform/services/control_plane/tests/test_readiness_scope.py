"""Readiness answers "send this replica requests?", not "is everything well?" (plan P5).

The backbone chaos drill (`tests/integration/test_backbone_outage_drill_live.py`) killed NATS and
found the control plane reporting itself unready: any failing startup check made `/readyz` 503, so
an orchestrator pulled every replica out of rotation over a *publisher*. Events are written to the
durable outbox first and published afterwards, so a broker outage delays delivery and refuses
nothing — the API was working perfectly while nothing could reach it.

Readiness now depends on the checks a request actually needs (the datastore, the credential, the
coordinator behind rate limits and leases). Everything else still turns `/health` degraded, which
is what the alerts read.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "a-real-secret-token-value")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    cp_app._run_startup_checks()
    assert cp_app._startup_checks["db"] == "ok"
    return cp_app


def _health(cp) -> dict:
    return TestClient(cp.app).get("/health").json()


def _readyz(cp) -> int:
    return TestClient(cp.app).get("/readyz").status_code


def _fix(cp, monkeypatch, **checks: str) -> None:
    """Pin the startup checks, and stop the probes from re-running them."""
    monkeypatch.setattr(cp, "_startup_checks", {**cp._startup_checks, **checks})
    monkeypatch.setattr(cp, "_run_startup_checks", lambda **_kw: None)


@pytest.mark.parametrize(
    ("check", "value"),
    [
        ("event_publisher", "fail: the NATS event backbone needs the 'nats-py' package"),
        ("event_publisher", "fail: nats: no servers available for connection"),
        ("registry", "warn: no enabled models"),
        ("identity_federation", "fail: trust file is invalid"),
    ],
)
def test_a_replica_stays_in_rotation_for_what_it_can_serve_without(cp, monkeypatch, check, value):
    _fix(cp, monkeypatch, **{check: value})
    assert _readyz(cp) == 200, f"{check} took the replica out of rotation"
    body = _health(cp)
    assert body["ready"] is True
    assert body["status"] == "degraded", "the failure is still reported and still alerts"
    assert body["startup_checks"][check] == value


@pytest.mark.parametrize(
    ("check", "value"),
    [
        ("db", "fail: connection refused"),
        ("token", "missing"),
        ("token", "weak"),
        ("coordinator", "fail: redis is unreachable"),
    ],
)
def test_a_replica_leaves_rotation_when_a_request_could_not_be_served(
    cp, monkeypatch, check, value
):
    _fix(cp, monkeypatch, **{check: value})
    assert _readyz(cp) == 503
    assert _health(cp)["ready"] is False


def test_an_unreadable_store_is_not_ready_whatever_the_checks_said(cp, monkeypatch):
    """The checks ran once at boot; the store can go away afterwards."""
    _fix(cp, monkeypatch)
    monkeypatch.setattr(cp, "_pending_approvals_count", lambda: None)
    assert _readyz(cp) == 503
    body = _health(cp)
    assert body["ready"] is False and body["status"] == "degraded"


def test_nothing_checked_yet_is_not_ready(cp, monkeypatch):
    """`all()` over an empty dict is vacuously true: a process that has checked nothing must not
    be handed traffic."""
    monkeypatch.setattr(cp, "_startup_checks", {})
    monkeypatch.setattr(cp, "_run_startup_checks", lambda **_kw: None)
    assert _readyz(cp) == 503
    assert _health(cp)["status"] == "starting"


def test_a_healthy_replica_is_ready(cp, monkeypatch):
    _fix(cp, monkeypatch)
    assert _readyz(cp) == 200
    body = _health(cp)
    assert body["ready"] is True and body["status"] in ("ok", "degraded")


# ── a failing relay is the platform's own evidence that the bus is gone ───────


def test_a_failing_relay_makes_health_degraded_but_keeps_the_replica_in_rotation(cp, monkeypatch):
    _fix(cp, monkeypatch)
    monkeypatch.setattr(cp, "EVENT_RELAY_SECONDS", 1.0)
    monkeypatch.setattr(cp, "_relay_last_error", "nats: no servers available for connection")
    body = _health(cp)
    assert body["status"] == "degraded"
    assert body["ready"] is True  # the outbox holds the events; the API serves
    assert _readyz(cp) == 200
    assert body["runtime"]["event_relay_error"]


def test_a_failing_relay_makes_the_probes_re_run_the_checks(cp, monkeypatch):
    """Startup checks used to re-run only when one was already failing, so a replica that started
    healthy never noticed the broker going away."""
    runs: list[bool] = []
    monkeypatch.setattr(cp, "_startup_checks", {k: "ok" for k in cp._startup_checks})
    monkeypatch.setattr(cp, "_startup_checked_at", 0.0)  # the interval has passed
    monkeypatch.setattr(cp, "_run_startup_checks", lambda **kw: runs.append(kw.get("recheck")))

    cp._recheck_failed_startup()
    assert runs == [], "healthy checks were re-run for nothing"

    monkeypatch.setattr(cp, "EVENT_RELAY_SECONDS", 1.0)
    monkeypatch.setattr(cp, "_relay_last_error", "nats: no servers available for connection")
    cp._recheck_failed_startup()
    assert runs == [True]


def test_a_backbone_that_is_not_there_is_reported_even_though_nothing_failed(cp, monkeypatch):
    """A relay batch stops at the first unreachable answer, so `failed` is 0 while nothing can be
    delivered at all. Reading only `failed` made `/health` say `ok` for 36 seconds of a measured
    outage, with the events still queued (backbone chaos drill, combined-failure section)."""
    _fix(cp, monkeypatch)
    monkeypatch.setattr(cp, "EVENT_RELAY_SECONDS", 1.0)
    result = {
        "claimed": 6,
        "published": 0,
        "failed": 0,
        "deferred": 6,
        "unavailable": "nats: no servers available for connection",
    }
    error = cp._relay_error_from(result)
    assert error and "no servers" in error and "6 event(s) waiting" in error

    monkeypatch.setattr(cp, "_relay_last_error", error)
    body = _health(cp)
    assert body["status"] == "degraded"
    assert body["ready"] is True and _readyz(cp) == 200  # the outbox holds them; the API serves


def test_an_outage_whose_client_gave_no_message_is_still_reported(cp):
    """`future.result(timeout)` raises a bare `TimeoutError`, so the reason can be the empty
    string. A falsy check on it is how a real outage published `status: ok`."""
    error = cp._relay_error_from(
        {"claimed": 6, "published": 0, "failed": 0, "deferred": 6, "unavailable": ""}
    )
    assert error and "6 event(s) waiting" in error


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"claimed": 0, "published": 0, "failed": 0}, None),
        ({"claimed": 3, "published": 3, "failed": 0}, None),
        ({"claimed": 3, "published": 1, "failed": 2}, "2 event(s) failed to publish"),
    ],
)
def test_a_relay_that_delivered_reports_nothing_to_alert_on(cp, result, expected):
    assert cp._relay_error_from(result) == expected


def test_with_the_relay_disabled_nothing_re_runs(cp, monkeypatch):
    runs: list[bool] = []
    monkeypatch.setattr(cp, "_startup_checks", {k: "ok" for k in cp._startup_checks})
    monkeypatch.setattr(cp, "_startup_checked_at", 0.0)
    monkeypatch.setattr(cp, "_run_startup_checks", lambda **kw: runs.append(kw.get("recheck")))
    monkeypatch.setattr(cp, "EVENT_RELAY_SECONDS", 0.0)
    monkeypatch.setattr(cp, "_relay_last_error", "stale error from before it was disabled")
    cp._recheck_failed_startup()
    assert runs == []
