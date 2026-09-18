"""``examlops.dataplane.streams.supervisor`` (ADR 0131 d7/d8, dataplane Plan 2 task A8).

Covers: reconcile (start / stop on pause, disable and removal / restart on a definition change,
never two runs of one stream at once), an unknown connector kind as an ``error`` stream that
never takes the supervisor down, push (``http``) streams left to the push route, full-jitter
backoff and its healthy reset, status redaction (no option values, redacted and bounded
errors), the pack-sync cadence, a catalog outage changing nothing, the per-stream dead-letter
sink, leader election with two supervisors on one real ``DbCoordinator`` (exactly one runs;
failover once the leader stops, and once it loses its lease), leases kept through ``stop()``
until ``release_leases()``, the ``CatalogView`` cache and the ``IngressStack`` close order.

Every test gets its own ``PLATFORM_DB`` via the autouse fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import replace
from typing import Any

import pytest
from prometheus_client import REGISTRY

from examlops.coordination import DbCoordinator
from examlops.dataplane.lease import LeaseHeartbeat, fence_after
from examlops.dataplane.streams import supervisor as sup_mod
from examlops.dataplane.streams.connectors import STATES
from examlops.dataplane.streams.dlq import DbDeadLetterSink, LoggingDeadLetterSink
from examlops.dataplane.streams.supervisor import (
    UNKNOWN_STATE_FALLBACK,
    CatalogUnavailable,
    CatalogView,
    IngressStack,
    StreamSupervisor,
    backoff_delay,
    leader_key,
)
from examlops.dataplane.streams.types import StreamBinding, StreamLimits
from examlops.dataplane.types import SpecError

# ── fakes ───────────────────────────────────────────────────────────────────────────────────


def _until(pred, timeout: float = 5.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def _binding(name: str = "s1", **kw: Any) -> StreamBinding:
    base: dict[str, Any] = {
        "project": "proj",
        "name": name,
        "connector": "fake",
        "model": "JPCP",
        "alias": "Production",
        "address": "topic-a",
        "connection": None,
        "options": {},
    }
    base.update(kw)
    return StreamBinding(**base)


class FakeConnector:
    """Runs until its stop event is set; counts runs and concurrent runs per stream."""

    kind = "fake"
    connection_kinds: tuple[str, ...] = ()

    def __init__(self, *, singleton: bool = False, fail_times: int = 0, error: str = "boom"):
        self.singleton = singleton
        self.fail_times = fail_times
        self.error = error
        self.lock = threading.Lock()
        self.runs: list[StreamBinding] = []
        self.active: dict[str, int] = {}
        self.max_active: dict[str, int] = {}
        self.stop_events: list[threading.Event] = []

    def running(self, name: str = "s1") -> int:
        with self.lock:
            return self.active.get(name, 0)

    def run(self, binding, ingress, stop_event, status_cb) -> None:
        with self.lock:
            self.runs.append(binding)
            self.stop_events.append(stop_event)
            n = self.active.get(binding.name, 0) + 1
            self.active[binding.name] = n
            self.max_active[binding.name] = max(self.max_active.get(binding.name, 0), n)
            failing = self.fail_times > 0
            if failing:
                self.fail_times -= 1
        try:
            status_cb("starting", None)
            if failing:
                raise RuntimeError(self.error)
            status_cb("running", None)
            stop_event.wait(30)
        finally:
            with self.lock:
                self.active[binding.name] -= 1


class Catalog:
    def __init__(self, *bindings: StreamBinding) -> None:
        self.rows = list(bindings)
        self.fail = False

    def __call__(self) -> list[StreamBinding]:
        if self.fail:
            raise RuntimeError("catalog down")
        return list(self.rows)

    def set(self, *bindings: StreamBinding) -> None:
        self.rows = list(bindings)


def _resolver(**kinds: Any):
    def resolve(kind: str) -> Any:
        try:
            return kinds[kind]
        except KeyError:
            raise SpecError(f"unknown stream connector {kind!r}; known: fake") from None

    return resolve


def _supervisor(catalog: Catalog, connector: Any, **kw: Any) -> StreamSupervisor:
    kw.setdefault("sync_pack", None)
    kw.setdefault("stop_join_s", 5.0)
    kw.setdefault("backoff_base_s", 0.01)
    kw.setdefault("backoff_cap_s", 0.05)
    return StreamSupervisor(
        ingress=object(),
        coord=kw.pop("coord", DbCoordinator()),
        list_bindings=catalog,
        resolve_connector=kw.pop("resolve", _resolver(fake=connector)),
        **kw,
    )


@pytest.fixture
def cleanup():
    sups: list[StreamSupervisor] = []
    yield sups.append
    for s in sups:
        s.stop(5.0)
        s.release_leases()


# ── reconcile ───────────────────────────────────────────────────────────────────────────────


def test_an_enabled_stream_starts_and_reports_running(cleanup):
    conn = FakeConnector()
    sup = _supervisor(Catalog(_binding(options={"topic_opt": "x"})), conn)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.running() == 1)
    assert _until(lambda: sup.status_of("proj", "s1")["state"] == "running")
    st = sup.status_of("proj", "s1")
    assert st["leader"] is False and st["singleton"] is False and st["restarts"] == 0
    assert st["connector"] == "fake" and st["model"] == "JPCP" and st["last_error"] is None
    assert set(st) >= {
        "project", "name", "connector", "model", "state", "detail", "since", "leader",
        "restarts", "last_error",
    }  # fmt: skip


def test_paused_disabled_and_removed_streams_stop_and_a_reenabled_one_restarts(cleanup):
    conn = FakeConnector()
    catalog = Catalog(_binding())
    sup = _supervisor(catalog, conn)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.running() == 1)

    catalog.set(_binding(state="paused"))  # pause proper is A8b: here paused means stopped
    sup.reconcile()
    assert conn.running() == 0 and conn.stop_events[0].is_set()
    st = sup.status_of("proj", "s1")
    assert st["state"] == "stopped" and st["detail"] == "stream is paused"

    catalog.set(_binding(state="enabled"))
    sup.reconcile()
    assert _until(lambda: conn.running() == 1)
    assert len(conn.runs) == 2

    catalog.set(_binding(state="disabled"))
    sup.reconcile()
    assert conn.running() == 0
    assert sup.status_of("proj", "s1")["detail"] == "stream is disabled"

    catalog.set(_binding(state="enabled"))
    sup.reconcile()
    assert _until(lambda: conn.running() == 1)
    catalog.set()  # removed from the catalog
    sup.reconcile()
    assert conn.running() == 0
    assert sup.status_of("proj", "s1") is None and sup.status() == []


def test_a_definition_change_restarts_the_stream_and_never_runs_two_at_once(cleanup):
    conn = FakeConnector()
    catalog = Catalog(_binding(address="topic-a"))
    sup = _supervisor(catalog, conn)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.running() == 1)
    for change in (
        {"address": "topic-b"},
        {"address": "topic-b", "connection": "kafka-2"},
        {"address": "topic-b", "connection": "kafka-2", "options": {"start": "latest"}},
        {
            "address": "topic-b",
            "connection": "kafka-2",
            "options": {"start": "latest"},
            "limits": StreamLimits(max_in_flight=3),
        },
    ):
        catalog.set(_binding(**change))
        sup.reconcile()
        assert _until(lambda: conn.running() == 1)
    assert len(conn.runs) == 5
    assert conn.runs[-1].limits.max_in_flight == 3 and conn.runs[-1].address == "topic-b"
    assert conn.max_active["s1"] == 1  # the replacement never overlapped the old run

    sup.reconcile()  # an unchanged definition is left running
    time.sleep(0.05)
    assert len(conn.runs) == 5


def test_an_unknown_kind_is_an_error_stream_and_never_stops_the_others(cleanup):
    conn = FakeConnector()
    catalog = Catalog(_binding("good"), _binding("bad", connector="no-such-kind"))
    sup = _supervisor(catalog, conn)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.running("good") == 1)
    bad = sup.status_of("proj", "bad")
    assert bad["state"] == "error"
    assert "unknown stream connector 'no-such-kind'" in bad["detail"]
    assert bad["last_error"] and "no-such-kind" in bad["last_error"]
    since = bad["since"]
    sup.reconcile()  # the same failure: the same entry, not a flapping one
    assert sup.status_of("proj", "bad")["since"] == since
    assert conn.running("good") == 1


def test_a_sink_factory_that_fails_is_an_error_stream(cleanup):
    conn = FakeConnector()

    def broken(binding):
        raise OSError("dlq table unavailable")

    sup = _supervisor(Catalog(_binding()), conn, dead_letter_sink_factory=broken)
    cleanup(sup)
    sup.reconcile()
    st = sup.status_of("proj", "s1")
    assert st["state"] == "error" and "OSError" in st["detail"]
    assert conn.runs == []


def test_push_streams_are_left_to_the_push_route(cleanup):
    conn = FakeConnector()
    sup = _supervisor(Catalog(_binding(connector="http")), conn)
    cleanup(sup)
    sup.reconcile()
    assert sup.status() == [] and conn.runs == []


def test_a_catalog_outage_changes_nothing(cleanup):
    conn = FakeConnector()
    catalog = Catalog(_binding())
    sup = _supervisor(catalog, conn)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.running() == 1)
    catalog.fail = True
    sup.reconcile()
    assert conn.running() == 1 and sup.status_of("proj", "s1")["state"] == "running"


def test_the_pack_sync_runs_first_and_then_at_most_every_interval(cleanup):
    now = [1000.0]
    calls: list[int] = []
    sup = _supervisor(
        Catalog(),
        FakeConnector(),
        sync_pack=lambda: calls.append(1) or {"errors": [], "conflicts": []},
        pack_sync_interval_s=60.0,
        clock=lambda: now[0],
    )
    cleanup(sup)
    sup.reconcile()
    sup.reconcile()
    assert len(calls) == 1
    now[0] += 59
    sup.reconcile()
    assert len(calls) == 1
    now[0] += 1
    sup.reconcile()
    assert len(calls) == 2


def test_a_successful_pack_sync_refreshes_the_model_schemas(cleanup):
    """I2: the pack sync is the ONE trigger for re-reading the pack. Without this, a model added
    to the pack at runtime got its stream started while the ingress still had no schema for it,
    so every message was forwarded raw and 422'd — reported `validation`, which never feeds drift,
    so nothing fired."""

    class _Ingress:
        def __init__(self) -> None:
            self.refreshed = 0

        def refresh_schema(self) -> None:
            self.refreshed += 1

    ingress = _Ingress()
    now = [1000.0]
    sup = StreamSupervisor(
        ingress,
        DbCoordinator(),
        list_bindings=Catalog(),
        sync_pack=lambda: {"errors": [], "conflicts": []},
        pack_sync_interval_s=60.0,
        clock=lambda: now[0],
    )
    cleanup(sup)
    sup.reconcile()
    sup.reconcile()  # inside the interval: no sync, so no refresh either
    assert ingress.refreshed == 1
    now[0] += 60
    sup.reconcile()
    assert ingress.refreshed == 2


def test_a_pack_sync_that_reported_errors_does_not_refresh_the_schemas(cleanup, caplog):
    """Re-review: a sync that reported errors read the pack *incompletely*, so it is not evidence
    about what the pack contains — the same reason A6 suppresses its removal sweep on any error.
    Refreshing on it could drop a live model's schema for a whole cycle."""

    class _Ingress:
        def __init__(self) -> None:
            self.refreshed = 0

        def refresh_schema(self) -> None:
            self.refreshed += 1

    ingress = _Ingress()
    now = [1000.0]
    report = {"errors": [{"file": "m.yaml", "entry": 0, "message": "bad"}], "conflicts": []}
    sup = StreamSupervisor(
        ingress,
        DbCoordinator(),
        list_bindings=Catalog(),
        sync_pack=lambda: report,
        pack_sync_interval_s=60.0,
        clock=lambda: now[0],
    )
    cleanup(sup)
    with caplog.at_level(logging.WARNING, logger="examlops.dataplane.streams.supervisor"):
        sup.reconcile()
    assert ingress.refreshed == 0
    assert "the model schemas were not refreshed" in caplog.text

    report["errors"] = []  # the next clean sync refreshes
    now[0] += 60
    sup.reconcile()
    assert ingress.refreshed == 1


def test_a_failed_pack_sync_refreshes_nothing_and_a_failing_refresh_is_survivable(cleanup):
    class _Ingress:
        def __init__(self, boom: bool) -> None:
            self.refreshed = 0
            self.boom = boom

        def refresh_schema(self) -> None:
            self.refreshed += 1
            if self.boom:
                raise OSError("the pack directory went away")

    def _sup_with(ingress, sync_pack):
        return StreamSupervisor(
            ingress, DbCoordinator(), list_bindings=Catalog(), sync_pack=sync_pack
        )

    def _boom():
        raise RuntimeError("pack dir missing")

    failed = _Ingress(boom=False)
    sup = _sup_with(failed, _boom)
    cleanup(sup)
    sup.reconcile()
    assert failed.refreshed == 0  # nothing was re-read, so nothing to refresh

    raising = _Ingress(boom=True)
    sup2 = _sup_with(raising, lambda: {"errors": [], "conflicts": []})
    cleanup(sup2)
    sup2.reconcile()  # must not raise: the previous schemas stay usable
    assert raising.refreshed == 1


def test_an_ingress_without_the_refresh_seam_is_fine(cleanup):
    synced: list[int] = []
    sup = StreamSupervisor(
        object(),
        DbCoordinator(),
        list_bindings=Catalog(),
        sync_pack=lambda: synced.append(1) or {"errors": []},
    )
    cleanup(sup)
    sup.reconcile()  # `object()` has no refresh_schema: no AttributeError
    assert synced == [1]  # and the sync itself still ran


def test_a_failing_pack_sync_does_not_stop_the_reconcile(cleanup):
    conn = FakeConnector()

    def boom():
        raise RuntimeError("pack dir missing")

    sup = _supervisor(Catalog(_binding()), conn, sync_pack=boom)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.running() == 1)


def test_the_reconcile_loop_runs_on_its_interval(cleanup):
    conn = FakeConnector()
    catalog = Catalog()
    sup = _supervisor(catalog, conn, reconcile_interval_s=0.05)
    cleanup(sup)
    sup.start()
    catalog.set(_binding())
    assert _until(lambda: conn.running() == 1)
    catalog.set()
    assert _until(lambda: conn.running() == 0)


# ── backoff ─────────────────────────────────────────────────────────────────────────────────


def test_backoff_is_full_jitter_exponential_and_capped():
    assert backoff_delay(1, rng=lambda: 1.0) == 1.0
    assert backoff_delay(2, rng=lambda: 1.0) == 2.0
    assert backoff_delay(6, rng=lambda: 1.0) == 32.0
    assert backoff_delay(7, rng=lambda: 1.0) == 60.0  # capped at 60 s
    assert backoff_delay(10_000, rng=lambda: 1.0) == 60.0  # no overflow
    assert backoff_delay(5, rng=lambda: 0.0) == 0.0
    assert backoff_delay(3, rng=lambda: 0.5) == 2.0


def test_a_failing_connector_reconnects_with_backoff(cleanup):
    conn = FakeConnector(fail_times=3)
    sup = _supervisor(Catalog(_binding()), conn)
    cleanup(sup)
    attempts: list[int] = []
    real = sup._backoff
    sup._backoff = lambda attempt: attempts.append(attempt) or real(attempt)  # type: ignore[method-assign]
    sup.reconcile()
    assert _until(lambda: conn.running() == 1)
    assert attempts == [1, 2, 3]  # grows while the connector never got healthy
    st = sup.status_of("proj", "s1")
    assert st["restarts"] == 3 and st["state"] == "running"
    assert st["last_error"] == "RuntimeError: boom"


def test_the_failure_count_resets_after_healthy_running(cleanup):
    """A connector that had been `running` for `healthy_reset_s` starts again from attempt 1."""

    class Flaky(FakeConnector):
        def run(self, binding, ingress, stop_event, status_cb):
            with self.lock:
                self.runs.append(binding)
            status_cb("running", None)
            if len(self.runs) <= 3:
                raise RuntimeError("dropped")
            stop_event.wait(30)

    conn = Flaky()
    sup = _supervisor(Catalog(_binding()), conn, healthy_reset_s=0.0)
    cleanup(sup)
    attempts: list[int] = []
    real = sup._backoff
    sup._backoff = lambda attempt: attempts.append(attempt) or real(attempt)  # type: ignore[method-assign]
    sup.reconcile()
    assert _until(lambda: len(conn.runs) == 4)
    assert attempts == [1, 1, 1]


def test_a_connector_that_returns_without_being_stopped_is_reconnected(cleanup):
    class Quitter(FakeConnector):
        def run(self, binding, ingress, stop_event, status_cb):
            with self.lock:
                self.runs.append(binding)
            if len(self.runs) == 1:
                return  # a fatal error the connector reported by returning
            stop_event.wait(30)

    conn = Quitter()
    sup = _supervisor(Catalog(_binding()), conn)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: len(conn.runs) == 2)
    st = sup.status_of("proj", "s1")
    assert st["restarts"] == 1 and "exited without being stopped" in st["last_error"]


# ── status redaction ────────────────────────────────────────────────────────────────────────


def test_status_never_shows_option_values_and_redacts_and_bounds_errors(cleanup):
    secret_url = "https://svc:s3cr3t-pw@broker.example:9093/x"
    conn = FakeConnector(
        fail_times=100, error=f"cannot reach {secret_url} password=hunter2 " + "x" * 900
    )
    b = _binding(options={"sasl_password": "opt-value-hunter2", "reply_topic": "r"})
    sup = _supervisor(Catalog(b), conn, backoff_base_s=0.001, backoff_cap_s=0.001)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: (sup.status_of("proj", "s1") or {}).get("restarts", 0) >= 2)
    st = sup.status_of("proj", "s1")
    text = json.dumps(sup.status())
    assert "opt-value-hunter2" not in text  # option values: never
    assert st["option_keys"] == ["reply_topic", "sasl_password"]  # key names only
    assert "s3cr3t-pw" not in text and "hunter2" not in text
    assert len(st["last_error"]) <= 300 and st["last_error"].startswith(
        "RuntimeError: cannot reach"
    )


def test_connector_state_is_mirrored_into_the_gauge(cleanup):
    name = f"g-{uuid.uuid4().hex[:8]}"
    conn = FakeConnector()
    catalog = Catalog(_binding(name))
    sup = _supervisor(catalog, conn)
    cleanup(sup)
    sup.reconcile()

    def gauge(state: str) -> float | None:
        labels = {"project": "proj", "stream": name, "state": state}
        return REGISTRY.get_sample_value("dataplane_stream_connector_state", labels)

    assert _until(lambda: gauge("running") == 1.0)
    catalog.set()
    sup.reconcile()
    # Live finding D8: the stream is gone from the catalog, so its series goes too. It used to
    # stand at `stopped 1` for the life of the process, and a deleted stream is indistinguishable
    # there from a stream that is down.
    assert gauge("stopped") is None and gauge("running") is None


def test_a_connector_that_stops_says_so_in_the_log(cleanup, caplog):
    """Live finding D8: the supervisor logged ``started (fake)`` and never a matching line when a
    stream's run ended, so a whole shutdown was invisible in the service log."""
    conn = FakeConnector()
    catalog = Catalog(_binding())
    sup = _supervisor(catalog, conn)
    cleanup(sup)
    with caplog.at_level(logging.INFO, logger="examlops.dataplane.streams.supervisor"):
        sup.reconcile()
        assert _until(lambda: (sup.status_of("proj", "s1") or {}).get("state") == "running")
        catalog.set()
        sup.reconcile()
        assert _until(lambda: "stopped (fake)" in caplog.text)
    assert "proj/s1: started (fake)" in caplog.text
    assert "proj/s1: stopped (fake)" in caplog.text


def test_a_state_change_zeroes_the_previous_state_before_the_series_is_cleared():
    """The one-hot rule (no two states at 1) and the clearing rule, on the metrics seam itself —
    the supervisor test above can only see the end of the sequence."""
    from examlops.dataplane.streams import metrics as m

    name = f"g-{uuid.uuid4().hex[:8]}"

    def gauge(state: str) -> float | None:
        labels = {"project": "proj", "stream": name, "state": state}
        return REGISTRY.get_sample_value("dataplane_stream_connector_state", labels)

    m.set_connector_state("proj", name, "running")
    m.set_connector_state("proj", name, "stopped")
    assert gauge("running") == 0.0 and gauge("stopped") == 1.0
    m.clear_connector_state("proj", name)
    assert gauge("running") is None and gauge("stopped") is None
    m.clear_connector_state("proj", name)  # idempotent: a second clear is not an error


def test_every_state_the_supervisor_emits_is_in_the_declared_vocabulary():
    """M1: ``connectors.STATES`` is the documented label set of
    ``dataplane_stream_connector_state``, and it used to omit ``standby`` — which the supervisor
    has always emitted for a follower. One vocabulary, and it has to be complete."""
    source = inspect.getsource(sup_mod)
    emitted = set(re.findall(r'_set_state\(\s*"([a-z_]+)"', source))
    emitted |= set(re.findall(r'_set_gauge\([^,]+,\s*"([a-z_]+)"\)', source))
    assert emitted, "the scan found no _set_state call at all"
    assert emitted <= set(STATES), sorted(emitted - set(STATES))
    assert "standby" in emitted and "standby" in STATES


def test_an_unknown_state_is_reported_but_never_reaches_the_metric(cleanup, caplog):
    """A third-party connector reporting a state nobody declared keeps the stream running and the
    stray value shows in ``status()`` — but ``state`` is a Prometheus label, so the gauge records
    the bounded fallback instead (re-review: warning it and publishing it anyway still let a
    connector widen the label set)."""
    name = f"u-{uuid.uuid4().hex[:8]}"
    conn = FakeConnector()
    sup = _supervisor(Catalog(_binding(name)), conn)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.running(name) == 1)
    stream = sup._streams[("proj", name)]

    def gauge(state: str) -> float | None:
        return REGISTRY.get_sample_value(
            "dataplane_stream_connector_state",
            {"project": "proj", "stream": name, "state": state},
        )

    with caplog.at_level(logging.WARNING, logger="examlops.dataplane.streams.supervisor"):
        stream._status_cb("teleporting", None)
    assert stream.status()["state"] == "teleporting"  # reported, never swallowed
    assert "unknown state 'teleporting'" in caplog.text
    assert gauge("teleporting") is None  # no unbounded label value was ever created
    assert gauge(UNKNOWN_STATE_FALLBACK) == 1.0
    assert gauge("running") == 0.0


# ── the dead-letter sink ────────────────────────────────────────────────────────────────────


def test_a_connector_taking_dlq_gets_a_fresh_instance_with_the_factorys_sink(cleanup):
    class SinkAware(FakeConnector):
        instances: list[SinkAware] = []

        def __init__(self, *, dlq=None):
            super().__init__()
            self.dlq = dlq
            SinkAware.instances.append(self)

    registered = SinkAware()
    sinks: dict[str, object] = {}

    def factory(binding):
        sinks[binding.name] = object()
        return sinks[binding.name]

    catalog = Catalog(_binding("a"), _binding("b"))
    sup = _supervisor(
        catalog, registered, dead_letter_sink_factory=factory, resolve=_resolver(fake=registered)
    )
    cleanup(sup)
    sup.reconcile()
    a, b = sup.connector_of("proj", "a"), sup.connector_of("proj", "b")
    assert a is not registered and b is not registered and a is not b
    assert a.dlq is sinks["a"] and b.dlq is sinks["b"]
    assert _until(lambda: a.running("a") == 1 and b.running("b") == 1)


def test_the_default_sink_is_the_db_sink(cleanup):
    """A8b: the supervisor's default ``dead_letter_sink_factory`` builds the database-backed sink,
    not the logging-only default — the seam stays injectable (the next test)."""

    class SinkAware(FakeConnector):
        def __init__(self, *, dlq=None):
            super().__init__()
            self.dlq = dlq

    sup = _supervisor(Catalog(_binding()), SinkAware())
    cleanup(sup)
    sup.reconcile()
    assert isinstance(sup.connector_of("proj", "s1").dlq, DbDeadLetterSink)


def test_the_logging_sink_can_still_be_injected(cleanup):
    class SinkAware(FakeConnector):
        def __init__(self, *, dlq=None):
            super().__init__()
            self.dlq = dlq

    sup = _supervisor(
        Catalog(_binding()), SinkAware(), dead_letter_sink_factory=lambda b: LoggingDeadLetterSink()
    )
    cleanup(sup)
    sup.reconcile()
    assert isinstance(sup.connector_of("proj", "s1").dlq, LoggingDeadLetterSink)


def test_a_connector_without_a_dlq_seam_runs_as_registered(cleanup):
    conn = FakeConnector()
    sup = _supervisor(Catalog(_binding()), conn)
    cleanup(sup)
    sup.reconcile()
    assert sup.connector_of("proj", "s1") is conn


# ── leader election ─────────────────────────────────────────────────────────────────────────


class Partitionable:
    """A real coordinator that can be cut off: every ``try_lock`` then fails (renewals included),
    as for a replica that stalled or lost its datastore."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.cut = threading.Event()
        self.unlocks: list[tuple[str, str]] = []

    def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
        if self.cut.is_set():
            return False
        return self.inner.try_lock(key, holder, ttl_s)

    def unlock(self, key: str, holder: str) -> None:
        self.unlocks.append((key, holder))
        self.inner.unlock(key, holder)


TTL = 5.0  # the shortest lease a supervisor (and a fenced heartbeat) accepts, per R24


def _pair(project: str = "proj", coord_a: Any = None):
    b = _binding(project=project)
    ca, cb = FakeConnector(singleton=True), FakeConnector(singleton=True)
    common = {"leader_ttl_s": TTL}
    a = _supervisor(
        Catalog(b), ca, coord=coord_a or DbCoordinator(), holder="host-a:1:aaaa", **common
    )
    bb = _supervisor(Catalog(b), cb, coord=DbCoordinator(), holder="host-b:2:bbbb", **common)
    return a, ca, bb, cb


def _one_leader(a, ca, b, cb) -> bool:
    return ca.running() + cb.running() == 1


def test_two_supervisors_elect_exactly_one_leader(cleanup):
    a, ca, b, cb = _pair()
    cleanup(a)
    cleanup(b)
    a.reconcile()
    b.reconcile()
    assert _until(lambda: _one_leader(a, ca, b, cb))
    time.sleep(TTL)  # renewals keep it that way across a whole TTL
    assert _one_leader(a, ca, b, cb)
    leader, follower = (a, b) if ca.running() else (b, a)
    assert leader.status_of("proj", "s1")["leader"] is True
    fst = follower.status_of("proj", "s1")
    assert fst["leader"] is False and fst["state"] == "standby"
    assert max(ca.max_active.get("s1", 0), cb.max_active.get("s1", 0)) == 1


def test_a_follower_takes_over_within_the_ttl_once_the_leader_stops(cleanup):
    a, ca, b, cb = _pair()
    cleanup(a)
    cleanup(b)
    a.reconcile()
    assert _until(lambda: ca.running() == 1)
    b.reconcile()
    assert _until(lambda: b.status_of("proj", "s1")["state"] == "standby")
    a.stop(5.0)
    a.release_leases()  # a clean stop gives the lease back at once
    started = time.monotonic()
    assert _until(lambda: cb.running() == 1, timeout=TTL + 1.0)
    assert time.monotonic() - started < TTL
    assert ca.running() == 0


def test_a_leader_that_loses_its_lease_stops_and_a_follower_takes_over(cleanup):
    cut = Partitionable(DbCoordinator())
    a, ca, b, cb = _pair(coord_a=cut)
    cleanup(a)
    cleanup(b)
    a.reconcile()
    assert _until(lambda: ca.running() == 1)
    b.reconcile()
    assert _until(lambda: b.status_of("proj", "s1")["state"] == "standby")

    cut.cut.set()  # a's renewals are refused from here: its heartbeat reports the lease lost
    assert _until(lambda: ca.running() == 0, timeout=TTL)  # on_lost stopped its connector
    assert _until(lambda: a.status_of("proj", "s1")["state"] == "standby")
    ast = a.status_of("proj", "s1")
    assert ast["leader"] is False
    # losing the lease is not a connector failure: no reconnect backoff, no error
    assert ast["restarts"] == 0 and ast["last_error"] is None
    lost_at = time.monotonic()
    # The row a last renewed expires within the TTL; the follower's next try (≤ TTL/3·1.25) wins.
    # +1 s: the DB coordinator stores whole-second expiries.
    assert _until(lambda: cb.running() == 1, timeout=TTL + 2.0)
    assert time.monotonic() - lost_at < TTL + 1.5
    assert b.status_of("proj", "s1")["leader"] is True


def test_leader_keys_carry_the_project_so_tenants_never_collide(cleanup):
    assert leader_key("a", "s") == "dataplane:stream-leader:a:s"
    assert leader_key("", "s") == "dataplane:stream-leader:_global:s"
    ca, cb = FakeConnector(singleton=True), FakeConnector(singleton=True)
    a = _supervisor(Catalog(_binding(project="a")), ca, leader_ttl_s=TTL, holder="h-a")
    b = _supervisor(Catalog(_binding(project="b")), cb, leader_ttl_s=TTL, holder="h-b")
    cleanup(a)
    cleanup(b)
    a.reconcile()
    b.reconcile()
    assert _until(lambda: ca.running() == 1 and cb.running() == 1)  # both lead: two keys


def test_stop_releases_a_lease_as_soon_as_its_run_actually_exits(cleanup):
    """R24/N3: the lease lives and dies on the run thread — `stop()` only signals and waits; it
    never unlocks on a run's behalf. So a connector that honours the stop signal promptly has
    already given its lease back by the time `stop()` returns (its own `finally` runs before the
    thread ends), while a connector still exiting after `stop()`'s bounded wait keeps its lease
    until `release_leases()` waits long enough for that same thread to finish."""
    spy = Partitionable(DbCoordinator())
    conn = FakeConnector(singleton=True)
    sup = _supervisor(Catalog(_binding()), conn, coord=spy, leader_ttl_s=TTL, holder="me:1:x")
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.running() == 1)
    sup.stop(5.0)  # the fake connector honours the stop event at once
    assert conn.running() == 0
    # the token is the holder plus a per-acquisition sequence number (see _next_token)
    assert spy.unlocks == [(leader_key("proj", "s1"), "me:1:x:1")]  # released by the run thread
    assert DbCoordinator().try_lock(leader_key("proj", "s1"), "other", TTL) is True
    DbCoordinator().unlock(leader_key("proj", "s1"), "other")
    sup.release_leases()  # nothing left to do: idempotent, no second unlock
    assert spy.unlocks == [(leader_key("proj", "s1"), "me:1:x:1")]


def test_a_slow_to_stop_run_keeps_its_lease_past_stops_own_timeout(cleanup):
    """The supervisor never releases a lease itself: a connector that lingers past `stop()`'s
    bounded wait keeps the coordinator lock held (renewed) until its own thread actually exits,
    however long that takes — `release_leases()` is only a second, separately bounded wait for
    the same thread, not a release-on-its-behalf."""
    spy = Partitionable(DbCoordinator())
    gate = threading.Event()

    class SlowToExit(FakeConnector):
        def run(self, binding, ingress, stop_event, status_cb) -> None:
            with self.lock:
                self.runs.append(binding)
                n = self.active.get(binding.name, 0) + 1
                self.active[binding.name] = n
            status_cb("running", None)
            stop_event.wait(30)
            gate.wait(10)  # lingers well past a short stop() timeout (e.g. flushing a commit)
            with self.lock:
                self.active[binding.name] -= 1

    conn = SlowToExit(singleton=True)
    sup = _supervisor(Catalog(_binding()), conn, coord=spy, leader_ttl_s=TTL, holder="me:1:x")
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.running() == 1)
    sup.stop(0.2)  # far shorter than the connector needs to actually return
    assert spy.unlocks == []  # the thread has not exited: the lease is still held
    assert DbCoordinator().try_lock(leader_key("proj", "s1"), "other", TTL) is False
    gate.set()  # let the connector actually finish
    sup.release_leases(5.0)
    assert spy.unlocks == [(leader_key("proj", "s1"), "me:1:x:1")]
    assert DbCoordinator().try_lock(leader_key("proj", "s1"), "other", TTL) is True


def test_a_reconcile_stop_releases_the_lease_at_once(cleanup):
    spy = Partitionable(DbCoordinator())
    conn = FakeConnector(singleton=True)
    catalog = Catalog(_binding())
    sup = _supervisor(catalog, conn, coord=spy, leader_ttl_s=TTL, holder="me:1:x")
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.running() == 1)
    catalog.set(_binding(state="paused"))
    sup.reconcile()
    assert spy.unlocks == [(leader_key("proj", "s1"), "me:1:x:1")]


def test_the_default_holder_is_host_pid_uuid():
    sup = StreamSupervisor(object(), DbCoordinator(), list_bindings=Catalog(), sync_pack=None)
    host, pid, tail = sup.holder.rsplit(":", 2)
    assert host and pid.isdigit() and len(tail) == 8


# ── the catalog view ────────────────────────────────────────────────────────────────────────


def test_the_catalog_view_caches_and_rate_limits_misses():
    now = [0.0]
    reads: list[int] = []
    rows = [_binding()]

    def read():
        reads.append(1)
        return list(rows)

    view = CatalogView(read, ttl_s=10.0, miss_refresh_s=1.0, clock=lambda: now[0])
    assert view.get("proj", "s1") is not None and len(reads) == 1
    assert view.get("proj", "s1") is not None and len(reads) == 1  # cached
    assert view.get("proj", "nope") is None and len(reads) == 1  # a miss right after a read
    now[0] = 0.5
    assert view.get("proj", "nope") is None and len(reads) == 1  # rate-limited
    rows.append(_binding("nope"))
    now[0] = 1.5
    assert view.get("proj", "nope") is not None and len(reads) == 2  # a new stream is seen
    now[0] = 12.0
    view.get("proj", "s1")
    assert len(reads) == 3  # stale: re-read


def test_the_catalog_view_serves_a_stale_snapshot_for_a_while_then_refuses():
    now = [0.0]
    state = {"fail": False}

    def read():
        if state["fail"]:
            raise RuntimeError("db down")
        return [_binding()]

    view = CatalogView(read, ttl_s=10.0, max_stale_s=60.0, clock=lambda: now[0])
    assert view.get("proj", "s1") is not None
    state["fail"] = True
    now[0] = 30.0
    assert view.get("proj", "s1") is not None  # a blip keeps the last good snapshot
    now[0] = 71.0
    with pytest.raises(CatalogUnavailable):
        view.get("proj", "s1")
    with pytest.raises(CatalogUnavailable):
        CatalogView(read).get("proj", "s1")  # nothing ever read


def test_invalidating_one_entry_re_reads_it_at_once():
    """Live finding D5: a pause the state route had just applied kept serving traffic for up to
    the view's TTL, because the push route reads the catalog through this cache. A state change
    made in THIS process now invalidates its own entry."""
    now = [0.0]
    reads: list[int] = []
    rows = [_binding()]

    def read():
        reads.append(1)
        return list(rows)

    view = CatalogView(read, ttl_s=10.0, miss_refresh_s=1.0, clock=lambda: now[0])
    assert view.get("proj", "s1").state == "enabled" and len(reads) == 1
    rows[:] = [_binding(state="paused")]
    now[0] = 2.0  # well inside the TTL: without the invalidation this still reads `enabled`
    assert view.get("proj", "s1").state == "enabled" and len(reads) == 1

    view.invalidate("proj", "s1")
    assert view.get("proj", "s1").state == "paused" and len(reads) == 2
    # Exactly one forced re-read; the ordinary TTL rules apply again afterwards.
    assert view.get("proj", "s1").state == "paused" and len(reads) == 2


def test_an_invalidated_entry_whose_re_read_fails_keeps_the_last_good_snapshot():
    now = [0.0]
    state = {"fail": False}

    def read():
        if state["fail"]:
            raise RuntimeError("db down")
        return [_binding()]

    view = CatalogView(read, ttl_s=10.0, miss_refresh_s=1.0, clock=lambda: now[0])
    assert view.get("proj", "s1") is not None
    state["fail"] = True
    view.invalidate("proj", "s1")
    assert view.get("proj", "s1") is not None  # a failed re-read is not a vanished stream


# ── the ingress stack ───────────────────────────────────────────────────────────────────────


def test_the_stack_closes_drift_then_spool_then_client_and_survives_a_failing_step():
    order: list[str] = []

    class Part:
        def __init__(self, name: str, fail: bool = False) -> None:
            self.name, self.fail = name, fail

        def close(self, timeout: float | None = None) -> None:
            order.append(self.name)
            if self.fail:
                raise RuntimeError("close failed")

    stack = IngressStack(
        object(), spool=Part("spool"), drift=Part("drift", fail=True), client=Part("client")
    )
    stack.close(1.0)
    stack.close(1.0)  # idempotent
    assert order == ["drift", "spool", "client"]


def test_the_production_stack_builds_and_closes(monkeypatch):
    stack = IngressStack.build(DbCoordinator())
    try:
        from examlops.dataplane.streams.client import RayPipelineClient
        from examlops.dataplane.streams.ingress import StreamIngress
        from examlops.dataplane.streams.telemetry import TelemetrySpool

        assert isinstance(stack.ingress, StreamIngress)
        assert isinstance(stack.spool, TelemetrySpool)
        assert isinstance(stack.client, RayPipelineClient)
    finally:
        stack.close(2.0)
    assert stack.client.closed


def test_status_text_redacts_and_truncates():
    assert sup_mod.status_text(None) is None
    out = sup_mod.status_text("https://u:pw-secret@h/x " + "y" * 500)
    assert "pw-secret" not in out and len(out) == 300


def test_definition_ignores_state_but_not_limits():
    b = _binding()
    assert sup_mod.definition_of(b) == sup_mod.definition_of(replace(b, state="paused"))
    assert sup_mod.definition_of(b) != sup_mod.definition_of(
        replace(b, limits=StreamLimits(rate_per_min=5))
    )


# ── fix round 1 ─────────────────────────────────────────────────────────────────────────────


class MemCoordinator:
    """``try_lock``/``unlock`` with exact monotonic expiries: the DB coordinator rounds leases to
    whole seconds, which would blur the sub-second margins these tests measure."""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._locks: dict[str, tuple[str, float]] = {}

    def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
        now = time.monotonic()
        with self._mu:
            current = self._locks.get(key)
            if current is None or current[1] <= now or current[0] == holder:
                self._locks[key] = (holder, now + ttl_s)
                return True
            return False

    def unlock(self, key: str, holder: str) -> None:
        with self._mu:
            current = self._locks.get(key)
            if current is not None and current[0] == holder:
                del self._locks[key]


class Flaky:
    """A coordinator that can be partitioned away: every call then raises (the review's probe)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.down = False

    def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
        if self.down:
            raise ConnectionError("coordinator unreachable")
        return self.inner.try_lock(key, holder, ttl_s)

    def unlock(self, key: str, holder: str) -> None:
        if self.down:
            raise ConnectionError("coordinator unreachable")
        self.inner.unlock(key, holder)


def test_a_partitioned_leader_fences_itself_so_two_never_run(cleanup):
    """I1: a leader whose coordinator only *errors* stops before its key can expire, so a
    follower that can reach the coordinator never runs alongside it."""
    mem = MemCoordinator()
    flaky = Flaky(mem)
    conn = FakeConnector(singleton=True)  # shared: `max_active` counts runs across both
    a = _supervisor(Catalog(_binding()), conn, coord=flaky, holder="host-a:1:a", leader_ttl_s=TTL)
    b = _supervisor(Catalog(_binding()), conn, coord=mem, holder="host-b:2:b", leader_ttl_s=TTL)
    cleanup(a)
    cleanup(b)
    a.reconcile()
    assert _until(lambda: conn.running() == 1 and a.status_of("proj", "s1")["leader"])
    b.reconcile()
    assert _until(lambda: b.status_of("proj", "s1")["state"] == "standby")

    flaky.down = True  # a is partitioned: renewals raise, they are never refused
    assert _until(lambda: b.status_of("proj", "s1")["leader"] is True, timeout=3 * TTL)
    assert _until(lambda: conn.running() == 1)
    time.sleep(1.0)  # a stays fenced while partitioned
    assert conn.max_active["s1"] == 1  # never two runs of the singleton at once
    ast = a.status_of("proj", "s1")
    assert ast["leader"] is False and ast["state"] == "standby"


class _ErrCoord:
    def __init__(self) -> None:
        self.unlocks = 0

    def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
        raise ConnectionError("down")

    def unlock(self, key: str, holder: str) -> None:
        self.unlocks += 1


def test_the_fence_loses_an_erroring_lease_at_fence_after():
    """R24: the fence deadline is `fence_after(ttl) == min(0.8*ttl, floor(ttl) - 2)`, timed from
    when the caller's acquisition was sent — not simply `0.8*ttl` (that is only the tighter bound
    for TTLs at or above 10 s; see `lease.fence_after`'s own docstring).

    P5: the lease's clock is INJECTED, so what decides this test is the deadline arithmetic and
    nothing else. It used to assert `fence_after(ttl) - 0.1 <= elapsed < ttl` against the wall
    clock — a ~2 s margin on a threaded assertion, which under a loaded `-n 8` run could decide
    the result on scheduling alone."""
    lost: list[float] = []
    ttl = TTL  # 5.0: the floor a fenced lease accepts
    expected = fence_after(ttl)
    now = [0.0]
    hb = LeaseHeartbeat(
        _ErrCoord(), "k", "me", ttl, on_lost=lambda: lost.append(now[0]),
        fence_on_error=True, clock=lambda: now[0],
    ).start()  # fmt: skip
    try:
        now[0] = expected - 0.01  # a hair BEFORE the deadline: not lost, however long we look
        time.sleep(0.2)
        assert hb.lost is False and lost == []
        now[0] = expected  # at it: lost, and before the key could expire (expected < ttl)
        assert _until(lambda: hb.lost)
        time.sleep(0.2)
    finally:
        hb.stop()
    assert lost == [expected]  # on_lost exactly once, at the deadline itself
    assert expected < ttl


def test_without_the_fence_an_erroring_lease_is_retried_as_before():
    """Plan 1's pull locks keep the default: an error is transient, never a loss."""
    hb = LeaseHeartbeat(_ErrCoord(), "k", "me", 0.3).start()
    try:
        time.sleep(0.9)
        assert hb.lost is False
    finally:
        hb.stop()


def test_a_hung_renewal_is_fenced_and_a_late_success_gives_the_lock_back():
    """P5: the renewal is held by an Event (not by timing), and the fence is driven by the
    injected clock — so neither half of this depends on a wall-clock margin."""
    gate, calling = threading.Event(), threading.Event()
    now = [0.0]

    class Hung:
        unlocks = 0

        def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
            calling.set()
            gate.wait(10)
            return True

        def unlock(self, key: str, holder: str) -> None:
            Hung.unlocks += 1

    hb = LeaseHeartbeat(Hung(), "k", "me", TTL, fence_on_error=True, clock=lambda: now[0]).start()
    try:
        assert calling.wait(TTL)  # the first renewal is in flight, and hangs
        assert hb.lost is False
        now[0] = fence_after(TTL)  # the watchdog fences it while that call still hangs
        assert _until(lambda: hb.lost)
        gate.set()  # the hung renewal now succeeds — after the fence
        assert _until(lambda: Hung.unlocks == 1)  # and gives the lock straight back
    finally:
        gate.set()
        hb.stop()


def test_a_crash_looping_leader_yields_to_a_healthy_follower(cleanup):
    """M6: a singleton leader failing locally hands its lease over instead of sitting on it."""
    mem = MemCoordinator()
    bad = FakeConnector(singleton=True, fail_times=10**6)
    good = FakeConnector(singleton=True)
    a = _supervisor(Catalog(_binding()), bad, coord=mem, holder="host-a:1:a", leader_ttl_s=TTL)
    b = _supervisor(Catalog(_binding()), good, coord=mem, holder="host-b:2:b", leader_ttl_s=TTL)
    cleanup(a)
    cleanup(b)
    a.reconcile()
    assert _until(lambda: len(bad.runs) >= 1)  # a was the leader first
    b.reconcile()
    assert _until(lambda: good.running() == 1, timeout=3 * TTL)
    st = a.status_of("proj", "s1")
    assert st["leader"] is False and st["restarts"] >= 4
    runs = len(bad.runs)
    time.sleep(0.5)
    assert len(bad.runs) == runs  # a sits out while b leads


def test_when_a_leader_yields():
    sup = StreamSupervisor(
        object(), MemCoordinator(), list_bindings=Catalog(), sync_pack=None,
        backoff_base_s=1.0, backoff_cap_s=60.0,
    )  # fmt: skip
    assert [sup._should_yield(n) for n in (1, 4, 5, 6)] == [False, False, True, True]
    sup = StreamSupervisor(
        object(), MemCoordinator(), list_bindings=Catalog(), sync_pack=None,
        backoff_base_s=10.0, backoff_cap_s=60.0,
    )  # fmt: skip
    assert [sup._should_yield(n) for n in (3, 4)] == [False, True]  # the cap is reached at 4


def test_a_lease_shorter_than_five_seconds_is_refused():
    """R24 raised the floor from 3 s to 5 s: below it, `fence_after`'s `floor(ttl) - 2` bound
    leaves less than a second of margin against the DB coordinator's whole-second expiry."""
    with pytest.raises(ValueError, match="at least 5"):
        StreamSupervisor(object(), MemCoordinator(), list_bindings=Catalog(), leader_ttl_s=4.9)


def test_concurrent_stale_readers_trigger_one_catalog_read():
    """M8: the stale check and the claim of the re-read are one critical section."""
    now = [0.0]
    reads: list[int] = []

    def read():
        reads.append(1)
        time.sleep(0.1)
        return [_binding()]

    view = CatalogView(read, ttl_s=10.0, clock=lambda: now[0])
    view.get("proj", "s1")
    now[0] = 30.0  # stale
    barrier = threading.Barrier(8)

    def reader():
        barrier.wait()
        view.get("proj", "s1")

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(reads) == 2  # the first read, and exactly one re-read


# ── fix round 2 (R24: fence-after-send, per-acquisition tokens, run-thread-owned lease) ───────


def test_a_stale_hung_renewal_can_never_unlock_a_newer_acquisition(cleanup):
    """N2 regression, reproduced at the level of the actual bug (the re-review's
    ``probe_a8rr_hang_unlock.py``): a heartbeat fenced by a datastore stall may still have a
    renewal in flight when a newer acquisition takes the key. The OLD code reused one holder
    across acquisitions, so the coordinator legitimately treated that late renewal as the
    replica's OWN re-entrant renewal, and its give-back then unlocked the NEW lease. A
    per-acquisition fencing token (``_Stream._acquire``/``_next_token``) fixes this at the root:
    the stale renewal now carries a DIFFERENT holder than any later acquisition, so a real
    holder-conditional coordinator refuses it outright once a newer one exists, and no unlock for
    the wrong holder is ever attempted."""
    mem = MemCoordinator()
    key = "test:n2:key"
    old_token, new_token = "host-a:1:a:1", "host-a:1:a:2"
    gate, hanging = threading.Event(), threading.Event()
    unlocks: list[tuple[str, str]] = []
    now = [0.0]  # P5: the fence is driven by an injected clock, never by a wall-clock margin

    class HangSecondCall:
        """The first call (the initial acquisition) succeeds at once; the second (a renewal)
        hangs behind ``gate`` — a datastore stall exactly when the fenced heartbeat tries to
        renew — then resolves against the shared ``mem`` once released."""

        def __init__(self) -> None:
            self.calls = 0

        def try_lock(self, k: str, holder: str, ttl_s: float) -> bool:
            self.calls += 1
            if self.calls == 2:
                hanging.set()
                gate.wait(5)
            return mem.try_lock(k, holder, ttl_s)

        def unlock(self, k: str, holder: str) -> None:
            unlocks.append((k, holder))
            mem.unlock(k, holder)

    coord = HangSecondCall()
    ttl = TTL  # 5.0: the floor a fenced lease accepts
    assert coord.try_lock(key, old_token, ttl)  # call #1: the initial acquisition
    hb = LeaseHeartbeat(
        coord, key, old_token, ttl, fence_on_error=True, join_s=0.3, clock=lambda: now[0]
    ).start()
    assert hanging.wait(ttl)  # call #2 (the renewal) is in flight and stalled
    now[0] = fence_after(ttl)  # the fence deadline passes while it hangs
    assert _until(lambda: hb.lost)

    # The run thread's own `finally` (``_Stream._give_back``) now stops this heartbeat (a bounded
    # join that cannot wait out the hung call) and gives back what THIS acquisition held, using
    # only its own token — exactly as the real supervisor does before it re-competes.
    hb.stop()
    coord.unlock(key, old_token)
    unlocks.clear()  # that give-back is expected; only the LATE renewal's effect matters below

    assert mem.try_lock(key, new_token, ttl) is True  # a newer acquisition now wins the key

    gate.set()  # the stale call #2 now finally runs, against the coordinator's real, current state
    time.sleep(0.3)  # give the (already-stopped) heartbeat thread time to act, if it would

    assert unlocks == []  # holder mismatch: the coordinator refused it, no unlock was ever issued
    assert mem.try_lock(key, "someone-else", ttl) is False  # the newer acquisition is untouched


def test_reconcile_never_replaces_a_singleton_while_its_old_run_still_holds_the_lease(cleanup):
    """N3 regression (the re-review's ``probe_a8rr_orphan_lease.py`` scenario): a definition
    change must never create a replacement while the previous run's thread — and so its lease —
    is still alive. Because the lease now lives on the run thread itself (its own ``finally``
    unlocks strictly before the thread ends), reconcile's alive check can never observe "not
    alive" while the lease is still held: a slow-to-exit connector just leaves the stream in
    ``error`` for a pass, with the real coordinator lock provably held throughout, until it
    actually exits and a later reconcile launches the replacement."""
    real = DbCoordinator()
    gate = threading.Event()
    key = leader_key("proj", "s1")

    class SlowToExit(FakeConnector):
        def run(self, binding, ingress, stop_event, status_cb) -> None:
            with self.lock:
                self.runs.append(binding)
            status_cb("running", None)
            stop_event.wait(30)
            gate.wait(10)  # lingers after being signalled (e.g. flushing a commit)

    conn = SlowToExit(singleton=True)
    catalog = Catalog(_binding())
    sup = _supervisor(catalog, conn, coord=real, leader_ttl_s=TTL, holder="me:1:x", stop_join_s=0.2)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: len(conn.runs) == 1)
    assert real.try_lock(key, "other", TTL) is False  # the lease is held

    catalog.set(_binding(limits=StreamLimits(rate_per_min=5)))  # a definition change
    sup.reconcile()  # step 1 signals stop and joins for only stop_join_s = 0.2 s — too short
    st = sup.status_of("proj", "s1")
    assert st["state"] == "error"  # never replaced while the old run is still alive
    assert real.try_lock(key, "other", TTL) is False  # still held: the old run has not exited

    gate.set()  # let the old run actually finish
    assert _until(lambda: real.try_lock(key, "other", TTL) is True, timeout=TTL + 2.0)
    real.unlock(key, "other")

    assert _until(lambda: (sup.reconcile(), len(conn.runs) == 2)[-1], timeout=5.0)
    assert conn.runs[-1].limits.rate_per_min == 5


def test_no_overlap_over_repeated_failovers_at_ttl_5_with_a_real_coordinator(cleanup):
    """A no-overlap stress test at the production floor TTL with the real ``DbCoordinator``:
    across several forced failovers (a normal reconcile-driven stop, as pause/disable does — not
    a full ``stop()``, which is a one-way supervisor shutdown), the singleton run count across
    both replicas never exceeds one at once — the core invariant I1, N2 and N3 all protect."""
    conn = FakeConnector(singleton=True)  # shared: max_active counts runs across both supervisors
    catalog_a, catalog_b = Catalog(_binding()), Catalog(_binding())
    a = _supervisor(catalog_a, conn, holder="host-a:1:aaaa", leader_ttl_s=TTL)
    b = _supervisor(catalog_b, conn, holder="host-b:2:bbbb", leader_ttl_s=TTL)
    cleanup(a)
    cleanup(b)
    a.reconcile()
    b.reconcile()
    assert _until(lambda: conn.running() == 1)
    for _ in range(4):
        leader, other, leader_catalog = (
            (a, b, catalog_a) if a.status_of("proj", "s1")["leader"] else (b, a, catalog_b)
        )
        leader_catalog.set(_binding(state="disabled"))  # force the current leader to stand down
        leader.reconcile()  # stops it and releases its lease before this call returns
        assert _until(lambda: conn.running() == 1, timeout=TTL + 2.0)  # the other took over
        leader_catalog.set(_binding())  # re-enable: the old leader restarts, now as a follower
        leader.reconcile()
        assert conn.max_active["s1"] == 1  # never two runs of the singleton at once, so far
    assert conn.max_active["s1"] == 1
