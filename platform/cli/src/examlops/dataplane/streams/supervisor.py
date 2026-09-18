"""Stream supervisor — runs every enabled stream's connector (ADR 0131 d7/d8, Plan 2 task A8).

Three pieces, each usable on its own:

* :class:`IngressStack` — the ONE shared request stack per process: ``TelemetrySpool`` over a
  ``DbTelemetrySink`` (hooks from :func:`metrics.spool_hooks`), ``RayPipelineClient``,
  ``DriftAggregator`` with the ``LoggingRetrainTrigger``, and the ``StreamIngress`` over all of
  them. :meth:`IngressStack.close` drains it in the one safe order: drift, then spool, then client.
* :class:`CatalogView` — a cached read of the stream catalog for the push path (ADR 0131 d2: the
  catalog is read by the supervisor, not per request).
* :class:`StreamSupervisor` — one thread per enabled stream, reconciled against the catalog.

**Threads.** Each enabled stream whose connector is a registered :class:`StreamConnector` runs on
its own daemon thread, which calls ``connector.run(binding, ingress, stop_event, status_cb)``.
A run that raises — or returns while nobody asked it to stop — is restarted after a full-jitter
exponential backoff (base 1 s, cap 60 s): ``uniform(0, min(cap, base·2ⁿ⁻¹))`` for the n-th
consecutive failure, the count reset once the connector had been reporting ``running`` for 60 s.
HTTP push streams (connector ``http``) have no thread: the service's push route *is* their
connector, so the supervisor does not manage them at all.

**Leader election** (singleton connectors only). The goal: no two runs of a singleton stream
ever overlap, under any datastore stall, timing or exit path.

* **Acquire first.** ``LeaseHeartbeat`` only renews, so a stream thread first calls
  ``coord.try_lock("dataplane:stream-leader:{project}:{stream}", token, ttl)`` (TTL 15 s, at least
  5). The project keeps two tenants' streams of one name apart; the unscoped one is ``_global``.
* **A fencing token per acquisition.** ``token`` is ``{host:pid:uuid8}:{n}`` — the supervisor's
  holder plus a sequence number, fresh for every attempt and never reused. The heartbeat renews,
  and the run unlocks, only with its own token; both coordinators condition ``try_lock`` and
  ``unlock`` on the holder, so an old lease's late renewal or unlock can never touch a newer
  acquisition — this replica's own included.
* **Self-fencing.** The heartbeat is fenced (``fence_on_error=True``): with no successful renewal
  for :func:`examlops.dataplane.lease.fence_after` seconds after the last one was *sent* —
  ``min(0.8·TTL, floor(TTL) − 2)``, which stays ahead of the DB coordinator's whole-second expiry
  — the lease is lost and ``on_lost`` stops the connector run, before a follower may take the key.
* **The lease lives on the run thread.** Acquire → start the heartbeat → run the connector →
  ``finally``: stop the heartbeat (bounded join) and ``unlock(key, own token)``. The supervisor
  only signals a run and waits for its thread; it never releases a lease on a run's behalf, so a
  lease is given back only once its connector has returned. :meth:`StreamSupervisor.stop` and
  :meth:`StreamSupervisor.release_leases` both signal and wait (bounded).
* **Followers** report ``standby`` and retry every TTL/3, jittered ±25 %. A leader that loses its
  lease goes back to ``standby`` and competes again with a new token. A leader that fails 5 times
  in a row (or reaches the backoff cap) gives its lease back and sits out one TTL.

**Reconcile** every 10 s (injectable): a new enabled stream starts; a disabled one stops; a
**paused** one is paused *in place* when its connector exposes a ``pause()``/``resume()`` seam
(Kafka: the assignment pauses, the consumer keeps polling and heartbeating, so group membership
survives) — the run is left alive, never restarted, and resumed in place the moment the row is
``enabled`` again (A8b); a connector with no such seam is simply stopped, like ``disabled``, and
started fresh once the row is ``enabled`` again. :meth:`StreamSupervisor.pause_stream`/
:meth:`resume_stream` apply the same policy at once, ahead of the next reconcile, for the
process's own live run — the service's ``POST /streams/{name}/state`` route calls them. A
singleton connector that implements ``pause()`` keeps running (and so keeps its leader lease)
while paused; one that does not is stopped like any other connector and, being a singleton,
competes for the lease again on its next start. A removed stream stops and is forgotten; a
changed definition (connector, model, alias, address, connection, options, limits) stops the old
run and starts a new one — never two runs of one stream in one process: the replacement starts
only once the old thread has exited; one still going after the bounded wait leaves the stream in
``error`` and its replacement waits for the next pass. ``sync_pack_streams()`` runs on the first
reconcile and then at most every 60 s. A catalog that cannot be read changes nothing: the running
streams keep running.

**Connectors** come from :func:`examlops.dataplane.streams.connectors.get`. A connector that takes
a ``dlq=`` keyword (``KafkaStreamConnector`` does) is built afresh per stream with the sink from
``dead_letter_sink_factory(binding)`` (default :class:`~examlops.dataplane.streams.dlq.
DbDeadLetterSink`, A8b — inject :class:`~examlops.dataplane.streams.dlq.LoggingDeadLetterSink` for
a stack that must not touch the database, e.g. a test); any other is shared as registered. An
unknown kind — or a sink that cannot be built — puts that stream in ``error`` with the reason; it
never takes the supervisor down.

**Status** (:meth:`StreamSupervisor.status`): project, name, connector, model, state, detail,
since, leader, restarts, last_error (redacted, ≤ 300 characters) plus ``singleton`` and the
binding's ``option_keys``. Option *values* are never reported — they are free-form connector
config. Every state change is mirrored into ``dataplane_stream_connector_state``, whose label set
is :data:`examlops.dataplane.streams.connectors.STATES` — the one vocabulary, ``standby``
included. A state outside it is reported in ``status()`` and logged, but the gauge records
:data:`UNKNOWN_STATE_FALLBACK` instead, so no connector can widen that label set.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
import itertools
import json
import logging
import random
import threading
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from examlops.dataplane.lease import LeaseHeartbeat
from examlops.dataplane.safety import redact
from examlops.dataplane.streams import metrics
from examlops.dataplane.streams.connectors import STATES
from examlops.dataplane.streams.dlq import DbDeadLetterSink, DeadLetterSink
from examlops.dataplane.streams.types import StreamBinding, display_project
from examlops.dataplane.types import SpecError

if TYPE_CHECKING:
    from examlops.coordination import Coordinator
    from examlops.dataplane.streams.client import RayPipelineClient
    from examlops.dataplane.streams.drift import DriftAggregator
    from examlops.dataplane.streams.ingress import StreamIngress
    from examlops.dataplane.streams.telemetry import TelemetrySpool

logger = logging.getLogger(__name__)

#: The connector kind served by the service's push route rather than by a supervised thread.
PUSH_CONNECTOR = "http"
LEADER_TTL_S = 15.0
#: The shortest leader lease a supervisor accepts.
MIN_LEADER_TTL_S = 5.0  # = lease.MIN_FENCED_TTL_S: the fence needs floor(TTL) - 2 >= 3 s
#: A singleton leader that failed this many times in a row yields its lease for one TTL.
YIELD_AFTER_FAILURES = 5
RECONCILE_INTERVAL_S = 10.0
PACK_SYNC_INTERVAL_S = 60.0
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 60.0
#: How long a connector must have been reporting ``running`` for its failure count to reset.
HEALTHY_RESET_S = 60.0
#: How long one reconcile waits for a stopping stream before leaving it for the next pass.
STOP_JOIN_S = 15.0
#: Ceiling on every status text (``detail``, ``last_error``).
STATUS_TEXT_MAX = 300
#: The actor pack syncs are audited as.
SUPERVISOR_ACTOR = "dataplane-supervisor"
#: What ``dataplane_stream_connector_state`` records when a connector reports a state outside
#: :data:`examlops.dataplane.streams.connectors.STATES`. The stray value is kept in ``status()``
#: (an operator reads that), but never becomes a metric label: the label set stays bounded.
UNKNOWN_STATE_FALLBACK = "error"
_LEASE_JOIN_S = 2.0
_MAX_BACKOFF_EXPONENT = 30
_LOG_EVERY_S = 60.0


# ── pure helpers ────────────────────────────────────────────────────────────────────────────


def leader_key(project: str, name: str) -> str:
    """``dataplane:stream-leader:{project}:{stream}``; the unscoped project is ``_global``."""
    return f"dataplane:stream-leader:{display_project(project)}:{name}"


def backoff_delay(
    attempt: int,
    *,
    base_s: float = BACKOFF_BASE_S,
    cap_s: float = BACKOFF_CAP_S,
    rng: Callable[[], float] = random.random,
) -> float:
    """Full-jitter exponential backoff before reconnect ``attempt`` (1-based)."""
    exponent = min(max(0, attempt - 1), _MAX_BACKOFF_EXPONENT)
    return max(0.0, rng()) * min(cap_s, base_s * (2**exponent))


def definition_of(binding: StreamBinding) -> str:
    """A stable fingerprint of everything that, changed, needs a restart of the stream's run."""
    return json.dumps(
        {
            "connector": binding.connector,
            "model": binding.model,
            "alias": binding.alias,
            "address": binding.address,
            "connection": binding.connection,
            "options": binding.options,
            "limits": dataclasses.asdict(binding.limits),
        },
        sort_keys=True,
        default=str,
    )


def status_text(value: object) -> str | None:
    """``value`` redacted and cut to :data:`STATUS_TEXT_MAX` characters (``None`` stays ``None``)."""
    if value is None:
        return None
    return redact(str(value))[:STATUS_TEXT_MAX]


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.UTC).isoformat(timespec="seconds")


def _default_sink(binding: StreamBinding) -> DeadLetterSink:
    """The default dead-letter sink for one stream.

    It ignores ``binding`` today — every stream dead-letters into the same table — but the seam is
    per-binding on purpose (review M13): it is where A7b's ``dlq_store_payload_default`` (whether
    a stream's payloads are stored at all) gets varied per stream, without the supervisor growing
    a second injection point. Do not "simplify" the signature away.
    """
    return DbDeadLetterSink()


def _default_list_bindings() -> list[StreamBinding]:
    from examlops.dataplane.streams.bindings import list_bindings

    return list_bindings()


def _default_sync_pack() -> Any:
    from examlops.dataplane.streams.bindings import sync_pack_streams

    return sync_pack_streams(actor=SUPERVISOR_ACTOR)


def _default_resolve(kind: str) -> Any:
    from examlops.dataplane.streams import connectors

    return connectors.get(kind)


def _with_sink(proto: Any, sink: DeadLetterSink) -> Any:
    """A per-stream instance carrying ``sink`` when the connector's class takes ``dlq=``, else the
    registered instance itself (it has no dead-letter seam to give a sink to)."""
    cls = type(proto)
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return proto
    if "dlq" not in params:
        return proto
    try:
        return cls(dlq=sink)
    except Exception as exc:  # noqa: BLE001 - a connector that cannot be rebuilt runs as registered
        logger.warning(
            "dataplane streams: could not build a %s connector with the stream's dead-letter "
            "sink (%s); using the registered instance",
            getattr(proto, "kind", "?"),
            type(exc).__name__,
        )
        return proto


# ── the shared ingress stack ────────────────────────────────────────────────────────────────


class IngressStack:
    """The one request stack a process shares between the push route and every connector."""

    def __init__(
        self,
        ingress: StreamIngress,
        *,
        spool: TelemetrySpool | Any,
        drift: DriftAggregator | Any,
        client: RayPipelineClient | Any,
    ) -> None:
        self.ingress = ingress
        self.spool = spool
        self.drift = drift
        self.client = client
        self._closed = False
        self._close_lock = threading.Lock()

    @classmethod
    def build(cls, coord: Coordinator | None = None) -> IngressStack:
        """The production stack. ``coord`` defaults to ``get_coordinator()``."""
        from examlops.coordination import get_coordinator
        from examlops.dataplane.streams.client import RayPipelineClient
        from examlops.dataplane.streams.drift import DriftAggregator, LoggingRetrainTrigger
        from examlops.dataplane.streams.ingress import StreamIngress
        from examlops.dataplane.streams.schema import ModelSchemaRegistry
        from examlops.dataplane.streams.telemetry import DbTelemetrySink, TelemetrySpool

        coordinator = coord if coord is not None else get_coordinator()
        on_drop, on_fail = metrics.spool_hooks()
        spool = TelemetrySpool(
            DbTelemetrySink(on_baseline=metrics.on_baseline), on_drop=on_drop, on_fail=on_fail
        )
        client: RayPipelineClient | None = None
        try:
            client = RayPipelineClient()
            drift = DriftAggregator(LoggingRetrainTrigger(), coordinator)
            # Its own registry, not the process-wide default: the supervisor refreshes THIS one
            # on every pack sync (review I2), so the schemas follow the pack the catalog follows.
            ingress = StreamIngress(client, spool, drift, ModelSchemaRegistry(), coord=coordinator)
        except BaseException:
            # Nothing may be left running behind a stack that was never handed out.
            if client is not None:
                client.close()
            spool.close(1.0)
            raise
        return cls(ingress, spool=spool, drift=drift, client=client)

    def close(self, timeout: float = 5.0) -> None:
        """Drain step 4, in order: ``drift.close(timeout)`` (its queued trigger jobs), then
        ``spool.close(timeout)`` (flush telemetry), then ``client.close()``. Each is bounded, and a
        failing one never skips the next. Idempotent."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        steps: tuple[tuple[str, Callable[[], None]], ...] = (
            ("drift", lambda: self.drift.close(timeout)),
            ("spool", lambda: self.spool.close(timeout)),
            ("client", self.client.close),
        )
        for what, step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - a drain step must not abort the drain
                logger.warning(
                    "dataplane streams: closing the %s failed (%s)", what, type(exc).__name__
                )


# ── the catalog view (push path) ────────────────────────────────────────────────────────────


class CatalogUnavailable(RuntimeError):
    """The stream catalog could not be read and there is no usable cached copy."""


class CatalogView:
    """A cached snapshot of the stream catalog, keyed ``(project, name)``.

    :meth:`get` answers from the snapshot, re-reading it when older than ``ttl_s`` — or, on a miss
    (a stream defined a moment ago), when older than ``miss_refresh_s``, so a stream of unknown
    names cannot turn the push route into a catalog read per request. A failed re-read keeps the
    last good snapshot for up to ``max_stale_s`` past its TTL (a datastore blip must not stop
    serving); past that — or with no snapshot at all — :meth:`get` raises
    :class:`CatalogUnavailable`. :meth:`refresh` always re-reads (the supervisor calls it each
    reconcile, so in the ``all`` role the push path shares its reads).
    """

    def __init__(
        self,
        list_fn: Callable[[], Iterable[StreamBinding]] | None = None,
        *,
        ttl_s: float = RECONCILE_INTERVAL_S,
        miss_refresh_s: float = 1.0,
        max_stale_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._list = list_fn or _default_list_bindings
        self._ttl = ttl_s
        self._miss_refresh = miss_refresh_s
        self._max_stale = max_stale_s
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: dict[tuple[str, str], StreamBinding] | None = None
        self._read_at = float("-inf")  # last successful read
        self._tried_at = float("-inf")  # last attempt
        self._last_warning = float("-inf")

    def refresh(self) -> list[StreamBinding]:
        """Read the catalog now (raising whatever the read raises) and cache it."""
        rows = list(self._list())
        with self._lock:
            self._snapshot = {(b.project, b.name): b for b in rows}
            self._read_at = self._tried_at = self._clock()
        return rows

    def get(self, project: str, name: str) -> StreamBinding | None:
        key = (project, name)
        with self._lock:
            # Check and claim the re-read in one critical section: when the snapshot goes stale,
            # exactly one of many concurrent readers re-reads; the rest answer from it.
            now = self._clock()
            snapshot, read_at, tried_at = self._snapshot, self._read_at, self._tried_at
            stale = snapshot is None or now - read_at >= self._ttl
            missing = snapshot is not None and key not in snapshot
            reread = (stale and now - tried_at >= min(self._ttl, self._miss_refresh)) or (
                missing and now - tried_at >= self._miss_refresh
            )
            if reread:
                self._tried_at = now
        if reread:
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 - reported as unavailability below
                self._warn(exc)
            with self._lock:
                snapshot, read_at = self._snapshot, self._read_at
        if snapshot is None or self._clock() - read_at >= self._ttl + self._max_stale:
            raise CatalogUnavailable("the stream catalog cannot be read")
        return snapshot.get(key)

    def _warn(self, exc: Exception) -> None:
        now = self._clock()
        if now - self._last_warning < _LOG_EVERY_S:
            return
        self._last_warning = now
        logger.warning("dataplane streams: catalog read failed (%s)", type(exc).__name__)


# ── one supervised stream ───────────────────────────────────────────────────────────────────


class _Stream:
    """One stream's run loop, lease and status. A new object per (re)start: threads and their
    stop events are single-use."""

    def __init__(
        self,
        sup: StreamSupervisor,
        binding: StreamBinding,
        *,
        connector: Any = None,
        singleton: bool = False,
        state: str = "starting",
        detail: str | None = None,
        last_error: str | None = None,
    ) -> None:
        self._sup = sup
        self.binding = binding
        self.definition = definition_of(binding)
        self.connector = connector
        self.singleton = singleton
        self.key = leader_key(binding.project, binding.name)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._run_stop: threading.Event | None = None
        self._token: str | None = None  # the current acquisition's fencing token
        self._lease_lost = threading.Event()
        self.thread: threading.Thread | None = None
        self.leader = False
        self.restarts = 0
        self.last_error = status_text(last_error)
        self._running_since: float | None = None
        self.state = ""
        self.detail: str | None = None
        self.since = sup._wall()
        self._set_state(state, detail)

    # ── lifecycle (the supervisor's side) ────────────────────────────────────────────────────

    @property
    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._loop, name=f"dataplane-stream-{self.binding.name}"[:60], daemon=True
        )
        self.thread.start()

    def signal_stop(self) -> None:
        """Ask the run to stop. The run itself gives its lease back on the way out — the
        supervisor never releases a lease on a run's behalf."""
        self._stop.set()
        with self._lock:
            run_stop = self._run_stop
        if run_stop is not None:
            run_stop.set()

    def join(self, timeout: float) -> bool:
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(max(0.0, timeout))
        return not self.alive

    # ── status ───────────────────────────────────────────────────────────────────────────────

    def _set_state(self, state: str, detail: str | None) -> None:
        gauge_state = state
        if state not in STATES:
            # A third-party connector reporting a state nobody declared: keep the stream running
            # (its own status is not worth a failure) and report the stray value in `status()`,
            # where it is one row an operator reads — but publish :data:`UNKNOWN_STATE_FALLBACK`
            # to the metric, because `state` is a Prometheus label and an unbounded label value
            # is a cardinality hazard, not a diagnosis (re-review).
            gauge_state = UNKNOWN_STATE_FALLBACK
            self._sup._warn(
                f"state:{state}",
                f"stream {display_project(self.binding.project)}/{self.binding.name} reported "
                f"the unknown state {state!r}; known: {', '.join(STATES)}; the gauge records "
                f"{UNKNOWN_STATE_FALLBACK!r}",
            )
        with self._lock:
            changed = state != self.state
            if changed:
                self.since = self._sup._wall()
            self.state = state
            self.detail = status_text(detail)
            if state == "running":
                if self._running_since is None:
                    self._running_since = self._sup._clock()
            else:
                self._running_since = None
        if changed:
            try:
                metrics.set_connector_state(self.binding.project, self.binding.name, gauge_state)
            except Exception:  # noqa: BLE001 - a metrics failure never breaks a stream
                logger.debug("dataplane streams: connector-state gauge failed", exc_info=True)

    def _status_cb(self, state: str, detail: str | None) -> None:
        """The connector's ``status_cb``. Never raises into the connector."""
        try:
            if state == "error" and detail:
                self.last_error = status_text(detail)
            self._set_state(str(state), detail)
        except Exception:  # noqa: BLE001
            logger.debug("dataplane streams: status callback failed", exc_info=True)

    def status(self) -> dict[str, Any]:
        b = self.binding
        with self._lock:
            return {
                "project": b.project,
                "name": b.name,
                "connector": b.connector,
                "model": b.model,
                "state": self.state,
                "detail": self.detail,
                "since": _iso(self.since),
                "leader": self.leader,
                "singleton": self.singleton,
                "restarts": self.restarts,
                "last_error": self.last_error,
                "option_keys": sorted(str(k) for k in (b.options or {})),
            }

    # ── the run loop (the stream thread) ─────────────────────────────────────────────────────
    #
    # The lease lives and dies on this thread: acquire (a fresh fencing token) → start its
    # heartbeat → run the connector → `finally`: stop the heartbeat (bounded join) and unlock with
    # the same token. Nothing else ever renews, unlocks or outlives it.

    def _loop(self) -> None:
        sup = self._sup
        try:
            while not self._stop.is_set():
                lease: tuple[str, LeaseHeartbeat] | None = None
                if self.singleton:
                    lease = self._acquire()
                    if lease is None:
                        self._set_state("standby", "another replica runs this stream")
                        self._stop.wait(sup._follower_wait())
                        continue
                try:
                    outcome = self._serve()
                finally:
                    if lease is not None:
                        self._give_back(*lease)
                if outcome == "yield":
                    self._stop.wait(sup._ttl)  # sit out one TTL: a healthy follower takes over
        except Exception as exc:  # noqa: BLE001 - never let a stream thread die silently
            self.last_error = status_text(f"{type(exc).__name__}: {exc}")
            logger.warning(
                "dataplane stream %s/%s: supervisor loop failed (%s)",
                display_project(self.binding.project),
                self.binding.name,
                type(exc).__name__,
            )
        finally:
            self._set_state("stopped", None)

    def _serve(self) -> str:
        """Run the connector — reconnecting with backoff — until the stream is stopped
        (``"stop"``), the lease is lost (``"lost"``) or a failing leader yields (``"yield"``)."""
        sup = self._sup
        attempt = 0
        while True:
            if self._stop.is_set():
                return "stop"
            if self._lease_lost.is_set():
                self._set_state("standby", "lost the leader lease")
                return "lost"
            run_stop = threading.Event()
            with self._lock:
                if self._stop.is_set():
                    return "stop"
                self._run_stop = run_stop
            if self._lease_lost.is_set():  # lost between acquiring and starting the run
                run_stop.set()
            error: str | None = None
            try:
                self.connector.run(self.binding, sup.ingress, run_stop, self._status_cb)
            except Exception as exc:  # noqa: BLE001 - reconnecting is the supervisor's job
                error = status_text(f"{type(exc).__name__}: {exc}")
            finally:
                with self._lock:
                    self._run_stop = None
                    healthy_since = self._running_since
            if self._stop.is_set():
                return "stop"
            if self._lease_lost.is_set():
                continue  # reported at the top of the loop
            # A run that raised, or returned without being asked to: reconnect after a backoff.
            if healthy_since is not None and sup._clock() - healthy_since >= sup._healthy_s:
                attempt = 0
            attempt += 1
            with self._lock:
                self.restarts += 1
                self.last_error = error or "the connector exited without being stopped"
            if self.singleton and sup._should_yield(attempt):
                # A leader failing locally (this host's network, say) must not keep the stream
                # from a healthy follower: give the lease back and sit out one TTL.
                self._set_state(
                    "standby", f"yielded the leader lease after {attempt} consecutive failures"
                )
                logger.warning(
                    "dataplane stream %s/%s: yielding the leader lease after %d failures",
                    display_project(self.binding.project),
                    self.binding.name,
                    attempt,
                )
                return "yield"
            delay = sup._backoff(attempt)
            self._set_state("retrying", f"reconnecting in {delay:.1f}s (attempt {attempt})")
            logger.warning(
                "dataplane stream %s/%s: connector stopped unexpectedly (%s); reconnecting "
                "in %.1fs",
                display_project(self.binding.project),
                self.binding.name,
                self.last_error,
                delay,
            )
            self._stop.wait(delay)

    def _acquire(self) -> tuple[str, LeaseHeartbeat] | None:
        """One election attempt with a fresh fencing token; on success the heartbeat is running
        and ``(token, heartbeat)`` is returned — the caller MUST hand it to :meth:`_give_back`."""
        sup = self._sup
        token = sup._next_token()
        sent = time.monotonic()  # the fence is timed from the send, not the reply
        try:
            acquired = bool(sup._coord_obj().try_lock(self.key, token, sup._ttl))
        except Exception as exc:  # noqa: BLE001 - an unanswered election is a standby
            sup._warn(f"lease:{self.key}", f"leader election failed ({type(exc).__name__})")
            return None
        if not acquired:
            return None
        self._lease_lost.clear()
        heartbeat = LeaseHeartbeat(
            sup._coord_obj(),
            self.key,
            token,
            sup._ttl,
            on_lost=lambda: self._on_lease_lost(token),
            join_s=min(_LEASE_JOIN_S, sup._ttl),
            # Self-fence before the key can expire (lease.fence_after), erroring or hanging
            # coordinator included: a partitioned leader stops before a follower may start.
            fence_on_error=True,
            acquired_at=sent,
        )
        with self._lock:
            self._token = token
            self.leader = True
        heartbeat.start()
        logger.info(
            "dataplane stream %s/%s: this replica is the leader",
            display_project(self.binding.project),
            self.binding.name,
        )
        return token, heartbeat

    def _give_back(self, token: str, heartbeat: LeaseHeartbeat) -> None:
        """Stop the heartbeat (bounded join), then unlock with this acquisition's own token — so
        a late renewal or unlock can never touch a newer acquisition's lock."""
        with self._lock:
            if self._token == token:
                self._token = None
            self.leader = False
        heartbeat.stop()
        try:
            self._sup._coord_obj().unlock(self.key, token)
        except Exception as exc:  # noqa: BLE001 - the TTL frees it anyway
            logger.warning(
                "dataplane streams: could not release %s (%s)", self.key, type(exc).__name__
            )

    def _on_lease_lost(self, token: str) -> None:
        """``LeaseHeartbeat.on_lost`` (a heartbeat thread): stop the current connector run — if
        the lost lease is still this run's current one."""
        with self._lock:
            if token != self._token:
                return
            self._lease_lost.set()
            self.leader = False
            run_stop = self._run_stop
        if run_stop is not None:
            run_stop.set()


# ── the supervisor ──────────────────────────────────────────────────────────────────────────


class StreamSupervisor:
    """Runs every enabled, connector-backed stream of the catalog; see the module docstring.

    ``ingress`` is the process's shared :class:`StreamIngress`; ``coord`` the coordinator for
    leader election (default ``get_coordinator()``, resolved on first use). Everything else is
    injectable for tests: ``list_bindings`` (default: the whole catalog), ``sync_pack`` (default
    :func:`~examlops.dataplane.streams.bindings.sync_pack_streams`; ``None`` disables),
    ``resolve_connector`` (default :func:`~examlops.dataplane.streams.connectors.get`), the
    intervals, ``holder``, ``rng``, ``clock`` (monotonic) and ``wall`` (epoch seconds).
    """

    def __init__(
        self,
        ingress: StreamIngress | Any,
        coord: Coordinator | None = None,
        *,
        dead_letter_sink_factory: Callable[[StreamBinding], DeadLetterSink] | None = None,
        list_bindings: Callable[[], Iterable[StreamBinding]] | None = None,
        sync_pack: Callable[[], Any] | None = _default_sync_pack,
        resolve_connector: Callable[[str], Any] | None = None,
        reconcile_interval_s: float = RECONCILE_INTERVAL_S,
        pack_sync_interval_s: float = PACK_SYNC_INTERVAL_S,
        leader_ttl_s: float = LEADER_TTL_S,
        backoff_base_s: float = BACKOFF_BASE_S,
        backoff_cap_s: float = BACKOFF_CAP_S,
        healthy_reset_s: float = HEALTHY_RESET_S,
        stop_join_s: float = STOP_JOIN_S,
        holder: str | None = None,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        from examlops.dataplane.streams.drift import replica_id

        if leader_ttl_s < MIN_LEADER_TTL_S:
            # The DB coordinator keeps whole-second expiries: a shorter lease is mostly rounding.
            raise ValueError(
                f"leader_ttl_s must be at least {MIN_LEADER_TTL_S:g} seconds, not {leader_ttl_s}"
            )
        self.ingress = ingress
        self._coord = coord
        self._sink_factory = dead_letter_sink_factory or _default_sink
        self._list_bindings = list_bindings or _default_list_bindings
        self._sync_pack = sync_pack
        self._resolve = resolve_connector or _default_resolve
        self._reconcile_s = reconcile_interval_s
        self._pack_sync_s = pack_sync_interval_s
        self._ttl = leader_ttl_s
        self._backoff_base = backoff_base_s
        self._backoff_cap = backoff_cap_s
        self._healthy_s = healthy_reset_s
        self._stop_join_s = stop_join_s
        self.holder = holder or replica_id()  # host:pid:uuid8 — one per supervisor
        self._rng = rng
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()  # guards _streams and _closing
        self._reconcile_lock = threading.Lock()  # one reconcile at a time
        self._streams: dict[tuple[str, str], _Stream] = {}
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sync: float | None = None
        self._last_warning: dict[str, float] = {}
        self._seq = itertools.count(1)
        self._seq_lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the reconcile loop; its first pass (pack sync included) runs at once."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._reconcile_loop, name="dataplane-stream-supervisor", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = STOP_JOIN_S) -> None:
        """Drain step 2 — stop intake: no more reconciles, every connector asked to stop (they
        commit what they must), and their threads joined within ``timeout`` in total. Each run
        gives its own lease back as it exits — never before its connector has returned — so a
        follower can never start alongside a run that is still going. Idempotent."""
        deadline = self._clock() + max(0.0, timeout)
        with self._lock:
            self._closing.set()
            streams = list(self._streams.values())
        for s in streams:
            s.signal_stop()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(max(0.0, deadline - self._clock()))
        for s in streams:
            if not s.join(deadline - self._clock()):
                logger.warning(
                    "dataplane stream %s/%s: connector still running after the stop timeout",
                    display_project(s.binding.project),
                    s.binding.name,
                )

    def release_leases(self, timeout: float = STOP_JOIN_S) -> None:
        """Drain step 5: signal every run to stop, then wait (bounded by ``timeout``) for them to
        exit — each run gives its lease back itself on the way out. The supervisor never unlocks
        on a run's behalf: a run still going when this returns keeps its lease (renewed, fenced
        on error) until it exits or its TTL runs out."""
        deadline = self._clock() + max(0.0, timeout)
        with self._lock:
            streams = list(self._streams.values())
        for s in streams:
            s.signal_stop()
        for s in streams:
            s.join(deadline - self._clock())

    # ── reconcile ────────────────────────────────────────────────────────────────────────────

    def _reconcile_loop(self) -> None:
        while not self._closing.is_set():
            try:
                self.reconcile()
            except Exception as exc:  # noqa: BLE001 - the loop must outlive one bad pass
                self._warn("reconcile", f"reconcile failed ({type(exc).__name__})")
            self._closing.wait(self._reconcile_s)

    def reconcile(self) -> None:
        """One pass against the catalog (see the module docstring). Public for tests."""
        with self._reconcile_lock:
            if self._closing.is_set():
                return
            self._maybe_sync_pack()
            try:
                bindings = list(self._list_bindings())
            except Exception as exc:  # noqa: BLE001 - an unreadable catalog changes nothing
                self._warn("catalog", f"catalog read failed ({type(exc).__name__})")
                return
            desired = {(b.project, b.name): b for b in bindings if b.connector != PUSH_CONNECTOR}
            with self._lock:
                current = dict(self._streams)

            # 1. Stop every live run that should no longer run as it is — except a `paused` one
            #    whose connector can be paused in place (A8b): left alive, paused, and resumed in
            #    place the moment it is `enabled` again — never stopped, never restarted. A
            #    connector with no pause seam is stopped, exactly like `disabled`.
            stopping: list[_Stream] = []
            for key, s in current.items():
                if not s.alive:
                    continue
                b = desired.get(key)
                if b is not None and definition_of(b) == s.definition:
                    if b.state == "enabled":
                        self._maybe_resume(s)
                        continue
                    if b.state == "paused" and self._pause_in_place(s):
                        continue
                s.signal_stop()
                stopping.append(s)
            deadline = self._clock() + self._stop_join_s
            for s in stopping:
                s.join(deadline - self._clock())

            # 2. Decide every key. A run gives its own lease back as its thread exits, so a
            #    replacement starts only once the previous run's thread has exited. One that is
            #    still going after the bounded wait above keeps the stream in `error` — its
            #    replacement is not started (the next pass looks again).
            with self._lock:
                if self._closing.is_set():
                    return
                for key in sorted(set(current) | set(desired)):
                    entry = current.get(key)
                    if entry is not None and entry.alive:
                        if entry.stop_requested:
                            entry._set_state(
                                "error",
                                f"the previous run has not stopped after {self._stop_join_s:g}s; "
                                "its replacement waits for it",
                            )
                        continue
                    b = desired.get(key)
                    if b is None:
                        if entry is not None:
                            self._streams.pop(key, None)
                            self._set_gauge(entry.binding, "stopped")
                        continue
                    if b.state != "enabled":
                        self._streams[key] = self._record(
                            entry, b, "stopped", f"stream is {b.state}"
                        )
                        continue
                    self._streams[key] = self._launch(entry, b)

    def _record(self, s: _Stream | None, b: StreamBinding, state: str, detail: str) -> _Stream:
        """A status-only entry (no thread); the existing one when it already says this."""
        if (
            s is not None
            and s.definition == definition_of(b)
            and s.state == state
            and s.detail == detail
        ):
            s.binding = b
            return s
        return _Stream(self, b, singleton=s.singleton if s else False, state=state, detail=detail)

    def _launch(self, s: _Stream | None, b: StreamBinding) -> _Stream:
        """Start ``b``'s run — or an ``error`` entry when its connector cannot be built."""
        try:
            proto = self._resolve(b.connector)
            sink = self._sink_factory(b)
            connector = _with_sink(proto, sink)
            singleton = bool(getattr(connector, "singleton", False))
        except SpecError as exc:
            reason = str(exc)
        except Exception as exc:  # noqa: BLE001 - one broken stream never stops the supervisor
            reason = f"cannot build the connector ({type(exc).__name__}: {exc})"
        else:
            new = _Stream(self, b, connector=connector, singleton=singleton)
            new.start()
            logger.info(
                "dataplane stream %s/%s: started (%s)",
                display_project(b.project),
                b.name,
                b.connector,
            )
            return new
        detail = status_text(reason)
        if s is not None and s.state == "error" and s.definition == definition_of(b):
            if s.detail == detail:
                return s  # the same failure as last pass: no new entry, no new log line
        logger.warning(
            "dataplane stream %s/%s: cannot start: %s", display_project(b.project), b.name, detail
        )
        return _Stream(self, b, state="error", detail=detail, last_error=reason)

    # ── runtime control (A8b) ────────────────────────────────────────────────────────────────

    def _pause_in_place(self, s: _Stream) -> bool:
        """``True`` (and paused) when ``s``'s connector exposes a working ``pause()`` seam — the
        reconcile then leaves the run alive rather than stopping it. ``False`` for a connector
        with no such seam, or one whose ``pause()`` itself raised: the caller stops the run
        instead, exactly as it would for a connector with no pause support at all."""
        pause = getattr(s.connector, "pause", None)
        if not callable(pause):
            return False
        try:
            pause(s.binding)
        except Exception as exc:  # noqa: BLE001 - a broken pause() must not wedge it unpaused
            logger.warning(
                "dataplane stream %s/%s: pause() failed (%s); stopping instead",
                display_project(s.binding.project),
                s.binding.name,
                type(exc).__name__,
            )
            return False
        return True

    def _maybe_resume(self, s: _Stream) -> None:
        """Resume ``s``'s connector in place when it exposes ``resume()``. Idempotent — safe to
        call on every reconcile pass whether or not the run was ever paused."""
        resume = getattr(s.connector, "resume", None)
        if callable(resume):
            try:
                resume(s.binding)
            except Exception as exc:  # noqa: BLE001 - the run keeps going either way
                logger.warning(
                    "dataplane stream %s/%s: resume() failed (%s)",
                    display_project(s.binding.project),
                    s.binding.name,
                    type(exc).__name__,
                )

    def pause_stream(self, project: str, name: str) -> None:
        """Runtime control (the service's ``POST /streams/{name}/state``): pause the live run of
        ``(project, name)`` at once, ahead of the next reconcile. A connector with a ``pause()``
        seam is paused in place — a singleton keeps its leader lease, since its run thread never
        returns; one with no such seam is stopped, and is started fresh once the catalog row is
        ``enabled`` again (by :meth:`resume_stream`, or the next reconcile). A no-op if the stream
        is not currently running in this process: the persisted state — set by the caller — is
        what every replica reconciles against."""
        with self._lock:
            s = self._streams.get((project, name))
        if s is None or not s.alive:
            return
        if not self._pause_in_place(s):
            s.signal_stop()

    def resume_stream(self, project: str, name: str) -> None:
        """The inverse of :meth:`pause_stream`: resume a live, paused-in-place run at once. A run
        this process stopped because its connector has no pause seam is not restarted here — the
        next reconcile launches it fresh, once the catalog row reads ``enabled``."""
        with self._lock:
            s = self._streams.get((project, name))
        if s is not None and s.alive:
            self._maybe_resume(s)

    def _maybe_sync_pack(self) -> None:
        if self._sync_pack is None:
            return
        now = self._clock()
        if self._last_sync is not None and now - self._last_sync < self._pack_sync_s:
            return
        self._last_sync = now
        try:
            report = self._sync_pack()
        except Exception as exc:  # noqa: BLE001 - a failed sync must not stop the reconcile
            self._warn("pack-sync", f"pack stream sync failed ({type(exc).__name__})")
            return
        errors = list(report.get("errors") or ()) if isinstance(report, dict) else []
        conflicts = list(report.get("conflicts") or ()) if isinstance(report, dict) else []
        # The pack sync is the ONE trigger for re-reading the pack (review I2): the stream catalog
        # and the model input schemas are two readings of the same model YAMLs, and a model added
        # at runtime whose schema the ingress never re-read would have its stream started and
        # every message 422'd (reported `validation`, which never feeds drift — so nothing fires).
        #
        # A sync that reported errors read a pack it could not read *completely*, so it is not
        # evidence about what the pack contains — the same reason A6 suppresses its pack-removal
        # sweep on any error. The refresh waits for the next clean sync; the schemas the ingress
        # already has stay in force (re-review).
        if not errors:
            refresh = getattr(self.ingress, "refresh_schema", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception as exc:  # noqa: BLE001 - the previous schemas stay usable
                    self._warn("schema-refresh", f"schema refresh failed ({type(exc).__name__})")
        if errors or conflicts:
            logger.warning(
                "dataplane streams: pack sync reported %d error(s) and %d conflict(s); "
                "the model schemas were not refreshed"
                if errors
                else "dataplane streams: pack sync reported %d error(s) and %d conflict(s)",
                len(errors),
                len(conflicts),
            )

    # ── status ───────────────────────────────────────────────────────────────────────────────

    def status(self) -> list[dict[str, Any]]:
        """Per supervised stream, sorted by (project, name); never an option value."""
        with self._lock:
            streams = [self._streams[k] for k in sorted(self._streams)]
        return [s.status() for s in streams]

    def status_of(self, project: str, name: str) -> dict[str, Any] | None:
        with self._lock:
            s = self._streams.get((project, name))
        return s.status() if s is not None else None

    def connector_of(self, project: str, name: str) -> Any:
        """The connector instance running ``project/name`` (A8b's pause/resume), or ``None``."""
        with self._lock:
            s = self._streams.get((project, name))
        return s.connector if s is not None else None

    # ── internals ────────────────────────────────────────────────────────────────────────────

    def _coord_obj(self) -> Coordinator:
        if self._coord is None:
            from examlops.coordination import get_coordinator

            self._coord = get_coordinator()
        return self._coord

    def _backoff(self, attempt: int) -> float:
        return backoff_delay(
            attempt, base_s=self._backoff_base, cap_s=self._backoff_cap, rng=self._rng
        )

    def _next_token(self) -> str:
        """A fencing token: ``{holder}:{n}``, unique for every election attempt and so for every
        acquisition — never reused, so an old lease's late renewal or unlock (both conditional
        on the holder) cannot touch a newer one."""
        with self._seq_lock:
            return f"{self.holder}:{next(self._seq)}"

    def _should_yield(self, attempt: int) -> bool:
        """A singleton leader yields after :data:`YIELD_AFTER_FAILURES` consecutive failures, or
        once its backoff has reached the cap."""
        exponent = min(max(0, attempt - 1), _MAX_BACKOFF_EXPONENT)
        capped = self._backoff_base * (2**exponent) >= self._backoff_cap
        return attempt >= YIELD_AFTER_FAILURES or capped

    def _follower_wait(self) -> float:
        return (self._ttl / 3.0) * (0.75 + 0.5 * self._rng())

    def _set_gauge(self, binding: StreamBinding, state: str) -> None:
        try:
            metrics.set_connector_state(binding.project, binding.name, state)
        except Exception:  # noqa: BLE001
            logger.debug("dataplane streams: connector-state gauge failed", exc_info=True)

    def _warn(self, what: str, message: str) -> None:
        now = self._clock()
        if now - self._last_warning.get(what, float("-inf")) < _LOG_EVERY_S:
            return
        self._last_warning[what] = now
        logger.warning("dataplane streams: %s", message)


__all__ = [
    "BACKOFF_BASE_S",
    "BACKOFF_CAP_S",
    "HEALTHY_RESET_S",
    "LEADER_TTL_S",
    "MIN_LEADER_TTL_S",
    "YIELD_AFTER_FAILURES",
    "PACK_SYNC_INTERVAL_S",
    "PUSH_CONNECTOR",
    "RECONCILE_INTERVAL_S",
    "STATUS_TEXT_MAX",
    "SUPERVISOR_ACTOR",
    "UNKNOWN_STATE_FALLBACK",
    "CatalogUnavailable",
    "CatalogView",
    "IngressStack",
    "StreamSupervisor",
    "backoff_delay",
    "definition_of",
    "leader_key",
    "status_text",
]
