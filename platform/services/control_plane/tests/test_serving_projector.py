"""The control plane compiles the serving snapshot: once, on change, and never in two places.

Plan P4.2 / ADR 0127. The projector runs in every control-plane replica and acts only in the one
holding the lease; it recompiles when a serving-relevant event lands in the outbox and on a slow
timer, and an MLflow outage costs a stale snapshot with a bounded retry rate — never a partial
one.
"""

from __future__ import annotations

import importlib
import time

import pytest
from cplane.projector import SnapshotProjector
from fastapi.testclient import TestClient


class _Coordinator:
    def __init__(self, grant: bool = True) -> None:
        self.grant = grant
        self.unlocked: list[str] = []

    def try_lock(self, key, holder, ttl):
        return self.grant

    def unlock(self, key, holder):
        self.unlocked.append(key)


class _Metrics:
    def __init__(self) -> None:
        self.generations: list[int] = []
        self.errors = 0

    def set_snapshot(self, generation, when):
        self.generations.append(generation)

    def record_snapshot_error(self):
        self.errors += 1


def _projector(*, grant=True, compile=None, watermark=None, interval=60.0):
    state = {"watermark": 1, "compiles": 0, "generation": 0}

    def _compile():
        state["compiles"] += 1
        state["generation"] += 1
        return state["generation"], True

    coordinator, metrics = _Coordinator(grant), _Metrics()
    projector = SnapshotProjector(
        coordinator=lambda: coordinator,
        holder="cp-1:snapshot",
        interval=interval,
        lease_seconds=30,
        metrics=metrics,
        compile_and_publish=compile or _compile,
        watermark=watermark or (lambda: state["watermark"]),
    )
    return projector, state, metrics, coordinator


def test_only_the_lease_holder_compiles():
    projector, state, _, _ = _projector(grant=False)
    assert projector.step() == "not_leader"
    assert state["compiles"] == 0


def test_it_compiles_once_then_waits_for_a_serving_change():
    projector, state, metrics, _ = _projector()

    assert projector.step() == "published"
    assert projector.step() == "idle"
    state["watermark"] = 2  # a traffic split / promotion landed in the outbox
    assert projector.step() == "published"

    assert state["compiles"] == 2
    assert metrics.generations == [1, 2]


def test_a_full_recompile_still_runs_on_the_timer():
    """What changed behind the platform's back (an alias moved in the MLflow UI) is found too."""
    projector, state, _, _ = _projector(interval=0.0)
    projector.step()
    assert projector.step() == "published"
    assert state["compiles"] == 2


def test_a_failed_compile_keeps_the_previous_generation_and_backs_off():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionError("MLflow is down")
        return calls["n"], True

    projector, state, metrics, _ = _projector(compile=flaky)
    projector.step()
    state["watermark"] = 2

    assert projector.step() == "error"
    assert projector.generation == 1  # still serving generation 1
    assert projector.step() == "backoff"  # not hammering MLflow
    assert metrics.errors == 1

    projector._next_retry = time.monotonic() - 1  # the backoff elapsed
    assert projector.step() == "published"
    assert projector.last_error is None and projector.generation == 3


def test_the_lease_is_released_on_shutdown():
    import threading

    projector, _, _, coordinator = _projector()
    stop = threading.Event()
    stop.set()
    projector.run(stop, tick=0.01)
    assert coordinator.unlocked == ["control-plane:serving-snapshot"]


# ─── wired into the service ───────────────────────────────────────────────────


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "a-real-secret-token-value")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    yield cp_app
    cp_app._snapshot_projector = None


def test_health_reports_the_projector(cp, monkeypatch):
    projector, _, _, _ = _projector()
    projector.step()
    monkeypatch.setattr(cp, "_snapshot_projector", projector)

    status = TestClient(cp.app).get("/health").json()["runtime"]["serving_snapshot"]

    assert status["enabled"] and status["leader"] and status["generation"] == 1


def test_the_projector_can_be_switched_off(cp, monkeypatch):
    monkeypatch.setattr(cp, "SNAPSHOT_SECONDS", 0.0)
    cp._start_snapshot_projector()
    assert cp._snapshot_projector is None
    assert TestClient(cp.app).get("/health").json()["runtime"]["serving_snapshot"] == {
        "enabled": False
    }
