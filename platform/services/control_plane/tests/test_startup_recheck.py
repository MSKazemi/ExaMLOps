"""A startup check that failed once does not pin the replica NotReady forever.

Found on the first `helm install` of the chart: the control plane booted while another replica was
still creating the schema, its one-shot database check failed, and `/readyz` answered 503 for as
long as the pod lived — although the table existed a second later. `helm --wait` timed out;
deleting the pod "fixed" it. Failed checks are now re-evaluated by the probes, rate-limited.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "a-real-secret-token-value")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    return cp_app


def _boot_with_db_down(cp, monkeypatch) -> None:
    real = cp._get_db

    def down():
        raise RuntimeError("relation pending_approvals does not exist")

    monkeypatch.setattr(cp, "_get_db", down)
    cp._run_startup_checks()
    monkeypatch.setattr(cp, "_get_db", real)


def test_a_failed_check_recovers_once_the_dependency_does(cp, monkeypatch):
    _boot_with_db_down(cp, monkeypatch)
    assert cp._startup_checks["db"].startswith("fail")
    monkeypatch.setattr(cp, "_startup_checked_at", 0.0)  # the recheck interval has passed

    body = TestClient(cp.app).get("/health").json()

    assert body["startup_checks"]["db"] == "ok"


def test_rechecks_are_rate_limited(cp, monkeypatch):
    """A probe every second must not re-run the whole battery every second."""
    _boot_with_db_down(cp, monkeypatch)
    runs: list[int] = []
    real = cp._run_startup_checks
    monkeypatch.setattr(cp, "_run_startup_checks", lambda **kw: runs.append(1) or real(**kw))

    client = TestClient(cp.app)
    client.get("/health")
    client.get("/readyz")

    assert runs == []  # just checked at "boot"; the interval has not passed


def test_passing_checks_are_not_rerun(cp, monkeypatch):
    cp._run_startup_checks()
    monkeypatch.setattr(cp, "_startup_checks", {k: "ok" for k in cp._startup_checks})
    monkeypatch.setattr(cp, "_startup_checked_at", 0.0)

    def never(**_kw):
        raise AssertionError("healthy checks re-ran on a probe")

    monkeypatch.setattr(cp, "_run_startup_checks", never)
    TestClient(cp.app).get("/health")


def test_readyz_itself_recovers_which_is_what_kubernetes_watches(cp, monkeypatch):
    """`/health` recovering is not enough: the readiness probe calls **`/readyz`**.

    The chart's `readinessProbe` targets `/readyz`, so the pod only leaves `0/1` if *that* endpoint
    starts answering 200. It does because `readyz()` delegates to `health()`, which re-runs failed
    checks — but nothing pinned that delegation, and removing it would restore the original bug in
    the only place it is visible to an orchestrator: the pod would stay NotReady for its whole life
    while `/health` reported itself recovered.
    """
    _boot_with_db_down(cp, monkeypatch)
    client = TestClient(cp.app)

    assert client.get("/readyz").status_code == 503  # the race has just been lost

    monkeypatch.setattr(cp, "_startup_checked_at", 0.0)  # the recheck interval has passed
    recovered = client.get("/readyz")

    assert recovered.status_code == 200, (
        f"the replica stays NotReady after its dependency came back: {recovered.json()}"
    )
    assert recovered.json()["startup_checks"]["db"] == "ok"
