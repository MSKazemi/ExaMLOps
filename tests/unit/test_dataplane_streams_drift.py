"""Unit tests for the stream drift aggregator (ADR 0130/0131, Plan 2, task A5).

Covers the rolling window (bridge parity: a full window at or above the threshold trips), the
submission arguments (dataset from ``drift_auto_retrain``, the idempotency key), the cross-replica
cooldown on a real ``DbCoordinator`` (two aggregators racing → exactly one submit), release of the
cooldown on a rejected or raising trigger, the kill switch, the no-config fallback, the bounded
executor's overflow, fail-open on a coordinator error, and the dry-run ``LoggingRetrainTrigger``.

Every test gets its own ``PLATFORM_DB`` via the autouse fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from examlops.coordination import DbCoordinator
from examlops.data import init_db
from examlops.data.audit import export_audit_events
from examlops.data.drift import set_drift_auto_retrain
from examlops.dataplane.streams.drift import (
    COOLDOWN_ENV,
    DATASET_ENV,
    DEFAULT_COOLDOWN_S,
    DEFAULT_THRESHOLD,
    DEFAULT_WINDOW,
    LOCK_PREFIX,
    THRESHOLD_ENV,
    WINDOW_ENV,
    BoundedExecutor,
    DriftAggregator,
    LoggingRetrainTrigger,
)

MODEL = "JPCP"

# ── helpers ──────────────────────────────────────────────────────────────────


class _Inline:
    """A TaskRunner that runs the job on the caller's thread — deterministic."""

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.jobs = 0

    def submit(self, fn: Callable[[], None]) -> bool:
        if not self.accept:
            return False
        self.jobs += 1
        fn()
        return True

    def shutdown(self, timeout: float = 5.0) -> None:
        return None


class _Trigger:
    def __init__(self, result: bool | Exception = True) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def submit(
        self, model: str, dataset: str, backend: str, reason: str, *, idempotency_key: str
    ) -> bool:
        with self._lock:
            self.calls.append(
                {
                    "model": model,
                    "dataset": dataset,
                    "backend": backend,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                }
            )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _MemCoordinator:
    """Minimal in-memory Coordinator (locks only matter here), optionally failing."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.locks: dict[str, str] = {}
        self.unlocks: list[tuple[str, str]] = []
        self.ttls: list[float] = []

    def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
        self.ttls.append(ttl_s)
        if self.fail:
            raise RuntimeError("coordinator down")
        if self.locks.get(key, holder) != holder:
            return False
        self.locks[key] = holder
        return True

    def unlock(self, key: str, holder: str) -> None:
        self.unlocks.append((key, holder))
        if self.locks.get(key) == holder:
            del self.locks[key]

    def first_seen(self, key: str, ttl_s: float) -> bool:
        return True

    def allow(self, bucket: str, limit: int, window_s: float) -> bool:
        return True


def _agg(trigger: Any, coord: Any, **kw: Any) -> DriftAggregator:
    kw.setdefault("cooldown_s", 300.0)
    kw.setdefault("window", 2)
    kw.setdefault("threshold", 0.5)
    kw.setdefault("now", lambda: 1000.0)
    kw.setdefault("executor", _Inline())
    return DriftAggregator(trigger, coord, **kw)


def _enable(model: str = MODEL, dataset: str = "DatasetX", cooldown_s: int = 300) -> None:
    init_db()
    set_drift_auto_retrain(model, enabled=True, dataset_name=dataset, cooldown_s=cooldown_s)


def _audit(action: str) -> list[dict[str, Any]]:
    rows = [r for r in export_audit_events() if r["action"] == action]
    for row in rows:
        row["details"] = json.loads(row["details"]) if row["details"] else {}
    return rows


# ── the window ───────────────────────────────────────────────────────────────


def test_a_partial_window_never_trips() -> None:
    _enable()
    trigger = _Trigger()
    agg = _agg(trigger, _MemCoordinator(), window=4)
    for _ in range(3):
        agg.observe(MODEL, True)
    assert trigger.calls == []
    assert agg.stats()["models"][MODEL]["failures"] == 3


def test_a_full_window_at_the_threshold_trips() -> None:
    _enable()
    trigger = _Trigger()
    agg = _agg(trigger, _MemCoordinator(), window=4, threshold=0.5)
    for failed in (True, False, True):
        agg.observe(MODEL, failed)
    assert trigger.calls == []
    agg.observe(MODEL, False)  # full: 2/4 = 0.5 >= 0.5
    assert len(trigger.calls) == 1


def test_successes_keep_the_rate_under_the_threshold() -> None:
    _enable()
    trigger = _Trigger()
    agg = _agg(trigger, _MemCoordinator(), window=4, threshold=0.75)
    for failed in (True, False) * 10:
        agg.observe(MODEL, failed)
    assert trigger.calls == []
    assert agg.stats()["models"][MODEL]["error_rate"] == 0.5


def test_the_window_rolls() -> None:
    _enable()
    trigger = _Trigger()
    agg = _agg(trigger, _MemCoordinator(), window=3, threshold=0.99)
    for failed in (True, False, False, True, True):
        agg.observe(MODEL, failed)
    assert trigger.calls == []  # window is [F, T, T]
    agg.observe(MODEL, True)  # [T, T, T]
    assert len(trigger.calls) == 1


def test_submit_arguments_come_from_the_row_and_the_clock() -> None:
    _enable(dataset="DatasetX")
    trigger = _Trigger()
    agg = _agg(trigger, _MemCoordinator(), now=lambda: 1000.0, cooldown_s=300.0)
    agg.observe(MODEL, True)
    agg.observe(MODEL, True)
    assert trigger.calls == [
        {
            "model": MODEL,
            "dataset": "DatasetX",
            "backend": "dataplane",
            "reason": "drift",
            "idempotency_key": f"dataplane:drift:{MODEL}:3",  # int(1000 // 300)
        }
    ]


def test_env_defaults_are_the_bridges_then_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (WINDOW_ENV, THRESHOLD_ENV, COOLDOWN_ENV):
        monkeypatch.delenv(name, raising=False)
    agg = DriftAggregator(_Trigger(), _MemCoordinator(), executor=_Inline())
    assert (agg._window, agg._threshold, agg.cooldown_s) == (
        DEFAULT_WINDOW,
        DEFAULT_THRESHOLD,
        DEFAULT_COOLDOWN_S,
    )
    assert (DEFAULT_WINDOW, DEFAULT_THRESHOLD, DEFAULT_COOLDOWN_S) == (50, 0.5, 300.0)
    monkeypatch.setenv(WINDOW_ENV, "7")
    monkeypatch.setenv(THRESHOLD_ENV, "0.25")
    monkeypatch.setenv(COOLDOWN_ENV, "42")
    agg = DriftAggregator(_Trigger(), _MemCoordinator(), executor=_Inline())
    assert (agg._window, agg._threshold, agg.cooldown_s) == (7, 0.25, 42.0)


# ── cross-replica cooldown on a real DbCoordinator ───────────────────────────


@pytest.mark.parametrize("executor", ["inline", "bounded"])
def test_two_aggregators_on_one_db_coordinator_submit_exactly_once(executor: str) -> None:
    _enable()
    trigger = _Trigger()
    aggs = [
        _agg(
            trigger,
            DbCoordinator(),
            window=1,
            threshold=1.0,
            holder=f"replica-{i}",
            executor=_Inline() if executor == "inline" else BoundedExecutor(),
        )
        for i in range(2)
    ]
    barrier = threading.Barrier(2)

    def race(agg: DriftAggregator) -> None:
        barrier.wait(timeout=10)
        agg.observe(MODEL, True, stream="s1", connector="http")

    threads = [threading.Thread(target=race, args=(agg,)) for agg in aggs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for agg in aggs:
        agg.close(timeout=10)
    assert len(trigger.calls) == 1
    assert sum(a.stats()["submitted"] for a in aggs) == 1
    assert sum(a.stats()["contended"] for a in aggs) == 1


def test_an_accepted_trigger_holds_the_cooldown_lock() -> None:
    _enable()
    coord = DbCoordinator()
    agg = _agg(_Trigger(True), coord, window=1, threshold=1.0, holder="me")
    agg.observe(MODEL, True)
    assert coord.try_lock(LOCK_PREFIX + MODEL, "someone-else", 60) is False


def test_a_rejected_trigger_releases_the_cooldown() -> None:
    _enable()
    coord = DbCoordinator()
    trigger = _Trigger(False)
    agg = _agg(trigger, coord, window=1, threshold=1.0, holder="me")
    agg.observe(MODEL, True, stream="s1", connector="kafka")
    assert coord.try_lock(LOCK_PREFIX + MODEL, "someone-else", 60) is True
    coord.unlock(LOCK_PREFIX + MODEL, "someone-else")
    agg.observe(MODEL, True)  # the in-process gate reopened too: the next breach retries
    assert len(trigger.calls) == 2
    failed = _audit("retrain_trigger_failed")
    assert failed and failed[0]["details"]["stream"] == "s1"
    assert failed[0]["details"]["connector"] == "kafka"
    assert failed[0]["details"]["reason"] == "drift"
    assert agg.stats()["rejected"] == 2


def test_a_raising_trigger_releases_the_cooldown() -> None:
    _enable()
    coord = _MemCoordinator()
    agg = _agg(_Trigger(RuntimeError("cp down")), coord, window=1, threshold=1.0, holder="me")
    agg.observe(MODEL, True)
    assert (LOCK_PREFIX + MODEL, "me") in coord.unlocks
    assert LOCK_PREFIX + MODEL not in coord.locks
    assert _audit("retrain_trigger_failed")[0]["details"]["error"] == "RuntimeError"


def test_within_the_cooldown_one_replica_submits_once() -> None:
    _enable()
    trigger = _Trigger()
    clock = {"t": 1000.0}
    agg = _agg(trigger, _MemCoordinator(), window=1, threshold=1.0, now=lambda: clock["t"])
    for _ in range(5):
        agg.observe(MODEL, True)
    assert len(trigger.calls) == 1
    clock["t"] += 301.0  # past the cooldown: the in-process gate reopens (the lock is ours)
    agg.observe(MODEL, True)
    assert len(trigger.calls) == 2
    assert trigger.calls[1]["idempotency_key"] == f"dataplane:drift:{MODEL}:4"


# ── per-model cooldown (R9.4) ────────────────────────────────────────────────


def test_a_rows_cooldown_sets_the_lock_ttl_and_the_idempotency_bucket() -> None:
    _enable(cooldown_s=60)
    trigger, coord = _Trigger(), _MemCoordinator()
    agg = _agg(trigger, coord, window=1, threshold=1.0, cooldown_s=300.0, now=lambda: 1000.0)
    agg.observe(MODEL, True)
    assert coord.ttls == [60.0]
    assert trigger.calls[0]["idempotency_key"] == f"dataplane:drift:{MODEL}:16"  # 1000 // 60


def test_a_rows_cooldown_sets_the_in_process_gate() -> None:
    _enable(cooldown_s=60)
    trigger = _Trigger()
    clock = {"t": 1000.0}
    agg = _agg(
        trigger,
        _MemCoordinator(),
        window=1,
        threshold=1.0,
        cooldown_s=300.0,
        now=lambda: clock["t"],
    )
    agg.observe(MODEL, True)
    clock["t"] += 61.0  # past the row's 60 s, well inside the default 300 s
    agg.observe(MODEL, True)
    assert len(trigger.calls) == 2


def test_without_a_row_the_default_cooldown_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DATASET_ENV, "FallbackDS")
    init_db()
    coord = _MemCoordinator()
    agg = _agg(_Trigger(), coord, window=1, threshold=1.0, cooldown_s=300.0)
    agg.observe(MODEL, True)
    assert coord.ttls == [300.0]


def test_a_disabled_rows_cooldown_governs_the_suppression_lock() -> None:
    init_db()
    set_drift_auto_retrain(MODEL, enabled=False, dataset_name="DatasetX", cooldown_s=45)
    coord = _MemCoordinator()
    _agg(_Trigger(), coord, window=1, threshold=1.0).observe(MODEL, True)
    assert coord.ttls == [45.0]
    assert _audit("retrain_suppressed")[0]["details"]["cooldown_s"] == 45.0


# ── kill switch and config ───────────────────────────────────────────────────


def test_the_kill_switch_suppresses_the_trigger() -> None:
    init_db()
    set_drift_auto_retrain(MODEL, enabled=False, dataset_name="DatasetX")
    trigger = _Trigger()
    coord = DbCoordinator()
    agg = _agg(trigger, coord, window=1, threshold=1.0, holder="me")
    agg.observe(MODEL, True, stream="s1", connector="http")
    assert trigger.calls == []
    rows = _audit("retrain_suppressed")
    assert len(rows) == 1
    assert rows[0]["target"] == MODEL
    assert rows[0]["details"]["suppressed_reason"] == "disabled"
    assert rows[0]["details"]["stream"] == "s1"
    assert rows[0]["details"]["connector"] == "http"
    # the suppression is the fleet's decision for this cooldown: the lock is kept
    assert coord.try_lock(LOCK_PREFIX + MODEL, "someone-else", 60) is False
    assert agg.stats()["suppressed"] == 1


def test_no_row_and_no_fallback_is_suppressed_as_no_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DATASET_ENV, raising=False)
    init_db()
    trigger = _Trigger()
    agg = _agg(trigger, _MemCoordinator(), window=1, threshold=1.0)
    agg.observe(MODEL, True)
    assert trigger.calls == []
    assert _audit("retrain_suppressed")[0]["details"]["suppressed_reason"] == "no_config"


def test_no_row_with_a_fallback_dataset_retrains_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(DATASET_ENV, "FallbackDS")
    init_db()
    trigger = _Trigger()
    agg = _agg(trigger, _MemCoordinator(), window=1, threshold=1.0)
    with caplog.at_level(logging.WARNING, logger="examlops.dataplane.streams.drift"):
        agg.observe(MODEL, True)
    assert [c["dataset"] for c in trigger.calls] == ["FallbackDS"]
    assert "no drift_auto_retrain row" in caplog.text


def test_an_unreadable_kill_switch_does_not_retrain(monkeypatch: pytest.MonkeyPatch) -> None:
    import examlops.data.drift as drift_data

    def boom(model: str) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(drift_data, "get_drift_auto_retrain", boom)
    trigger = _Trigger()
    coord = _MemCoordinator()
    agg = _agg(trigger, coord, window=1, threshold=1.0, holder="me")
    agg.observe(MODEL, True)
    assert trigger.calls == []
    assert LOCK_PREFIX + MODEL not in coord.locks  # released for the next breach


# ── never on the request thread, never raising ───────────────────────────────


def test_a_coordinator_error_fails_open() -> None:
    _enable()
    trigger = _Trigger()
    coord = _MemCoordinator(fail=True)
    agg = _agg(trigger, coord, window=1, threshold=1.0)
    agg.observe(MODEL, True)
    assert len(trigger.calls) == 1  # the idempotency key still dedups downstream
    assert coord.unlocks == []


def test_a_full_executor_drops_the_job_and_reopens_the_gate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _enable()
    trigger = _Trigger()
    runner = _Inline(accept=False)
    agg = _agg(trigger, _MemCoordinator(), window=1, threshold=1.0, executor=runner)
    with caplog.at_level(logging.WARNING, logger="examlops.dataplane.streams.drift"):
        agg.observe(MODEL, True)
    assert trigger.calls == []
    assert agg.stats()["dropped"] == 1
    assert "dropped" in caplog.text
    runner.accept = True
    agg.observe(MODEL, True)
    assert len(trigger.calls) == 1


def test_observe_never_raises() -> None:
    class _Exploding:
        def submit(self, fn: Callable[[], None]) -> bool:
            raise RuntimeError("executor bug")

        def shutdown(self, timeout: float = 5.0) -> None:
            return None

    _enable()
    agg = _agg(_Trigger(), _MemCoordinator(), window=1, threshold=1.0, executor=_Exploding())
    agg.observe(MODEL, True)
    assert agg.stats()["dropped"] == 1


def test_the_trigger_runs_off_the_observing_thread() -> None:
    _enable()
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class _Blocking:
        def submit(self, model: str, dataset: str, backend: str, reason: str, **kw: Any) -> bool:
            started.set()
            release.wait(timeout=10)
            calls.append(model)
            return True

    agg = _agg(_Blocking(), _MemCoordinator(), window=1, threshold=1.0, executor=BoundedExecutor())
    agg.observe(MODEL, True)  # returns while the trigger is still blocked
    assert started.wait(timeout=10)
    assert calls == []
    release.set()
    agg.close(timeout=10)
    assert calls == [MODEL]


def test_close_is_bounded_even_with_a_hung_trigger(caplog: pytest.LogCaptureFixture) -> None:
    """M5: a hung trigger cannot hold a drain up; after close, observe says so."""
    _enable()
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    class _Hung:
        def submit(self, model: str, dataset: str, backend: str, reason: str, **kw: Any) -> bool:
            started.set()
            release.wait(timeout=10)
            calls.append(model)
            return True

    agg = _agg(_Hung(), _MemCoordinator(), window=1, threshold=1.0, executor=BoundedExecutor())
    agg.observe(MODEL, True)
    assert started.wait(timeout=10)
    finished = threading.Event()
    closer = threading.Thread(target=lambda: (agg.close(timeout=0.05), finished.set()))
    closer.start()
    assert finished.wait(timeout=5)  # returned while the trigger is still blocked
    assert calls == []
    with caplog.at_level(logging.WARNING, logger="examlops.dataplane.streams.drift"):
        agg.observe("OTHER", True)
    assert "aggregator closed" in caplog.text
    assert "executor full" not in caplog.text
    assert agg.stats()["closed"] is True
    release.set()
    closer.join(timeout=5)


def test_warnings_are_throttled_per_message(caplog: pytest.LogCaptureFixture) -> None:
    """M6: a noisy condition inside the throttle window never hides a different one."""
    _enable()
    agg = _agg(_Trigger(), _MemCoordinator(fail=True), window=1, threshold=1.0)
    with caplog.at_level(logging.WARNING, logger="examlops.dataplane.streams.drift"):
        agg.observe(MODEL, True)  # lock unavailable → warned
        agg._executor = _Inline(accept=False)  # type: ignore[assignment]
        agg.observe("OTHER", True)  # executor full → a different key, also warned
        agg.observe("THIRD", True)  # executor full again → throttled
    assert caplog.text.count("cooldown lock unavailable") == 1
    assert caplog.text.count("executor full") == 1


@pytest.mark.parametrize(
    ("threshold", "window", "expected"),
    [(0.0, 4, 0.25), (-1.0, 2, 0.5), (1.5, 4, 1.0), (float("nan"), 4, DEFAULT_THRESHOLD)],
)
def test_an_out_of_range_threshold_is_clamped_with_a_warning(
    threshold: float, window: int, expected: float, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="examlops.dataplane.streams.drift"):
        agg = _agg(_Trigger(), _MemCoordinator(), window=window, threshold=threshold)
    assert agg.threshold == expected
    assert "threshold" in caplog.text


def test_a_window_below_one_is_clamped() -> None:
    agg = _agg(_Trigger(), _MemCoordinator(), window=0, threshold=0.5)
    assert agg.window == 1


def test_the_executors_threads_are_daemon_so_process_exit_is_bounded() -> None:
    """P3: ``ThreadPoolExecutor`` joins its (non-daemon) workers at interpreter exit, without a
    timeout, from a hook that runs BEFORE any ``atexit`` handler — so one hung retrain trigger
    would hang process exit and nothing we could register would bound it."""
    ex = BoundedExecutor()
    started = threading.Event()
    assert ex.submit(started.set) is True
    assert started.wait(5)
    assert ex._threads and all(t.daemon for t in ex._threads)
    ex.shutdown(timeout=1.0)


def test_a_failing_job_never_takes_its_worker_down() -> None:
    ex = BoundedExecutor(max_workers=1)
    done = threading.Event()
    assert ex.submit(lambda: (_ for _ in ()).throw(RuntimeError("boom"))) is True
    assert ex.submit(done.set) is True
    assert done.wait(5)  # the same worker served the next job
    ex.shutdown(timeout=1.0)


def test_shutdown_drops_queued_work_and_frees_its_slots() -> None:
    release = threading.Event()
    ex = BoundedExecutor(max_workers=1, max_pending=3)
    ran: list[int] = []
    assert ex.submit(lambda: release.wait(10)) is True  # occupies the one worker
    assert ex.submit(lambda: ran.append(1)) is True  # queued behind it
    ex.shutdown(timeout=0.05)  # returns while the first job still blocks
    release.set()
    time.sleep(0.2)
    assert ran == []  # the queued job was dropped, never run after shutdown


def test_the_bounded_executor_refuses_past_its_bound() -> None:
    release = threading.Event()
    ex = BoundedExecutor(max_workers=2, max_pending=2)
    assert ex.submit(lambda: release.wait(timeout=10)) is True
    assert ex.submit(lambda: release.wait(timeout=10)) is True
    assert ex.submit(lambda: None) is False
    release.set()
    ex.shutdown(timeout=10)
    assert ex.submit(lambda: None) is False


# ── the dry-run trigger ──────────────────────────────────────────────────────


def test_logging_trigger_audits_a_would_trigger_row_and_accepts() -> None:
    init_db()
    ok = LoggingRetrainTrigger().submit(
        MODEL, "DatasetX", "dataplane", "drift", idempotency_key="dataplane:drift:JPCP:3"
    )
    assert ok is True
    rows = _audit("dataplane_retrain_would_trigger")
    assert len(rows) == 1
    assert rows[0]["details"]["idempotency_key"] == "dataplane:drift:JPCP:3"
    assert _audit("retrain_triggered") == []
