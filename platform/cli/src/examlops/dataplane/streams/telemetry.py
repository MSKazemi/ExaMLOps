"""Telemetry seam for the dataplane streaming surface (ADR 0130/0131, Plan 2, task A3).

This module gives the streaming ingress (a later task) one place to hand off per-inference
telemetry without waiting on it: :class:`TelemetryRecord` is the frozen value object it builds,
:class:`TelemetrySpool` is the bounded, drop-on-full, thread-drained buffer that decouples the
reply path from the write path, and :class:`DbTelemetrySink` is the first (and for now only)
:class:`TelemetrySink` — writing drift/input-embedding snapshots into ``platform.db`` via
``examlops.data.drift``. A NATS sink plugs into the same :class:`TelemetrySink` seam later.

Behaviour carried over from the Dataplane bus bridge's ``_TelemetrySpool``/``_persist_inference_
telemetry`` (``platform/clients/dataplane_bus_bridge.py``, owned by another session — read, never
imported from here):

- reply first, telemetry after: the caller offers a record to the spool and moves on; a daemon
  thread drains it, off the request path.
- drops (queue full or closed) and failures (a sink write that raised) are counted, never
  silently lost.
- there is deliberately **no per-inference audit row** — nothing reads one, and it would take the
  platform-wide audit lock on the hot path for every prediction.
- the embedding is summarised (:class:`EmbeddingStats`) at the point of measurement; the raw
  vector never enters a :class:`TelemetryRecord` and is never persisted.

Bridge-parity gate (fix round 1, findings I2a/I2b): the bridge only ever offers telemetry from the
success path of ``_call_pipeline`` — a raised failure never reaches the spool. ``DbTelemetrySink``
reproduces that: **both** the drift snapshot and the input-embedding snapshot are gated on
``outcome == "ok"``; the drift write additionally needs a non-``None`` prediction, and the input
write additionally needs embedding stats with ``dim > 0`` (an empty embedding is never persisted,
matching the bridge's ``if embedding:``). Every other record is accepted and skipped — this task
does not touch the bridge's separate in-memory ``DriftTracker`` (error-rate → autopilot retrain),
which is out of scope for the telemetry seam.

Failure reporting (fix round 1, finding I1/I6): a sink's ``write_batch``/``write`` attempts every
record and never aborts a batch early on one bad record, but if any record failed it raises
:class:`TelemetryWriteError` naming how many. :class:`TelemetrySpool` counts exactly that many as
failed (falling back to the whole batch for a sink that raises something else), so
``stats()["failed"]`` and ``on_fail`` reflect real per-record failures, not just a stub's.
"""

from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from examlops.dataplane.streams.types import Outcome

logger = logging.getLogger(__name__)

#: Overrides the spool's default bounded queue size (``EXAMLOPS_DATAPLANE_TELEMETRY_QUEUE_MAX``).
#: Unset, empty, non-integer or non-positive all fall back to :data:`_QUEUE_MAX_DEFAULT`.
QUEUE_MAX_ENV = "EXAMLOPS_DATAPLANE_TELEMETRY_QUEUE_MAX"
_QUEUE_MAX_DEFAULT = 1000
_DEFAULT_BATCH = 100

# A baseline changes only when someone runs `exa drift input baseline`, so re-reading it from the
# DB on every write would be a SQLite hit per inference for a value that is near-constant — the
# same reasoning as the bridge's `_publish_input_baseline`. This module never touches a metrics
# registry itself (fix round 1, finding I5): it only reads the baseline, TTL-bounded, on the
# drainer thread, and hands the raw stats to the caller's `on_baseline` callback. A later metrics
# task (A5) owns turning that into Prometheus gauges, so the names are defined exactly once.
_BASELINE_TTL_SECONDS = 60.0
_baseline_seen_at: dict[str, float] = {}
_baseline_lock = threading.Lock()


def _refresh_baseline(model_name: str, on_baseline: Callable[[str, dict[str, Any]], None]) -> None:
    """At most once per TTL per model, read the recorded input-embedding baseline and hand it to
    ``on_baseline(model, stats)``. Never raises.

    A failed read does **not** stamp the TTL — the next write for this model retries immediately
    instead of waiting out the full TTL on a read that never actually happened.
    """
    now = time.monotonic()
    with _baseline_lock:
        last = _baseline_seen_at.get(model_name, float("-inf"))
        if now - last < _BASELINE_TTL_SECONDS:
            return
        _baseline_seen_at[model_name] = now
    try:
        from examlops.data.drift import get_input_baseline

        stats = get_input_baseline(model_name)
    except Exception:
        with _baseline_lock:
            _baseline_seen_at.pop(model_name, None)
        return
    if not stats:
        # No baseline recorded yet — the TTL stamp stands: there is nothing to hand the callback,
        # and re-checking every request for a model with no baseline would defeat the TTL.
        return
    try:
        on_baseline(model_name, stats)
    except Exception:  # noqa: BLE001 - a caller's hook must not break the drainer
        logger.warning("dataplane telemetry: on_baseline callback raised", exc_info=True)


@dataclass(frozen=True)
class EmbeddingStats:
    """Summary statistics of one input embedding — the raw vector never enters a
    :class:`TelemetryRecord`."""

    norm: float
    mean: float
    std: float
    dim: int

    @classmethod
    def from_vector(cls, vals: Sequence[float]) -> EmbeddingStats:
        """Compute stats from a raw embedding vector, byte-identical to the bridge's formula:
        ``mean = sum/n``, population ``std = sqrt(sum((v-mean)^2)/n)`` (``0.0`` for ``n <= 1``),
        ``norm = sqrt(sum(v*v))``. An empty vector yields all-zero stats with ``dim=0`` (never
        persisted by :class:`DbTelemetrySink` — see the module docstring)."""
        values = list(vals)
        n = len(values)
        if n == 0:
            return cls(norm=0.0, mean=0.0, std=0.0, dim=0)
        mean = sum(values) / n
        std = math.sqrt(sum((v - mean) ** 2 for v in values) / n) if n > 1 else 0.0
        norm = math.sqrt(sum(v * v for v in values))
        return cls(norm=norm, mean=mean, std=std, dim=n)


@dataclass(frozen=True, kw_only=True)
class TelemetryRecord:
    """One inference's telemetry, offered to a :class:`TelemetrySpool` after the reply is sent.

    ``model``/``alias`` carry the model's canonical YAML ``name`` exactly as written — no case
    folding, matching ``drift_status``. ``embedding`` is a summary, never the raw vector.

    There is deliberately **no ``tenant`` field** (review M7/I4). The ingress is the only producer
    and it has no tenant to give: a stream's tenancy unit is its binding's ``project``, which this
    record already carries, and a message may never name its own tenant. A field that could only
    ever be ``None`` reads as "the tenant was not known for this one", which is a different and
    false statement. When a real tenant source exists (the ADR 0131 d9 NATS sink), it gets added
    then, with something to put in it.

    ``outcome`` is kept even though the ingress offers ``ok`` records only: it is a real value on
    every record, the DB sink's ``!= "ok"`` guard is what keeps a future producer (or sink) from
    silently writing failure snapshots, and the NATS event schema carries it.
    """

    event_id: str
    ts: float
    project: str | None = None
    stream: str
    connector: str
    model: str
    alias: str
    model_version: str | None = None
    outcome: Outcome
    prediction: float | None = None
    embedding: EmbeddingStats | None = None
    job_id: str | None = None
    traceparent: str | None = None
    schema_version: int = 1


class TelemetrySink(Protocol):
    """Anything that can persist a batch of :class:`TelemetryRecord`.

    A sink offering only ``write(record)`` (no ``write_batch``) is also accepted by
    :class:`TelemetrySpool` — the shim agreed with examlops-83: the spool calls it once per
    record, in its own ``try``, and reports the failure count the same way ``write_batch`` does
    (see :class:`TelemetryWriteError`).
    """

    def write_batch(self, records: Sequence[TelemetryRecord]) -> None: ...

    def close(self) -> None: ...


class TelemetryWriteError(RuntimeError):
    """Raised by a sink after attempting every record in a batch, when one or more failed.

    ``failed`` is how many records in the batch did not persist; ``total`` is the batch size.
    Records that succeeded are not re-attempted and are not counted as failed —
    :class:`TelemetrySpool` adds exactly ``failed`` to its failure counter, not the whole batch.
    The message never carries payload or prediction values, only the counts.
    """

    def __init__(self, *, failed: int, total: int) -> None:
        self.failed = failed
        self.total = total
        super().__init__(f"{failed}/{total} record(s) failed to persist")


class DbTelemetrySink:
    """Persists inference telemetry into ``platform.db`` via ``examlops.data.drift``.

    Each record is written with its own connection (the helpers each own theirs — there is no
    ``platform_db`` primitive for a multi-record transaction, and this task adds none), so a batch
    is a loop, not one transaction. Every record in the batch is attempted regardless of an
    earlier one failing; if any failed, :meth:`write_batch` raises :class:`TelemetryWriteError`
    naming exactly how many (never the whole batch just because one record was bad), and logs
    once for the batch — a count and the exception type, never a payload or a prediction value.
    There is deliberately no per-inference audit row.

    ``on_baseline(model, stats)``, if given, is called at most once per
    :data:`_BASELINE_TTL_SECONDS` per model, on this sink's own call stack (the spool's drainer
    thread in production) — never from the request/ingress path. This module defines no
    Prometheus gauge itself; a later metrics task turns ``stats`` into whatever gauges it owns.
    """

    def __init__(self, *, on_baseline: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        from examlops.data import init_db

        init_db()
        self._on_baseline = on_baseline

    def write_batch(self, records: Sequence[TelemetryRecord]) -> None:
        failed = 0
        last_exc_type = ""
        for record in records:
            try:
                self._write_one(record)
            except Exception as exc:  # noqa: BLE001 - one bad record must not abort the batch
                failed += 1
                last_exc_type = type(exc).__name__
        if failed:
            logger.warning(
                "dataplane telemetry: %d/%d record(s) failed to persist (last error: %s)",
                failed,
                len(records),
                last_exc_type,
            )
            raise TelemetryWriteError(failed=failed, total=len(records))

    def _write_one(self, record: TelemetryRecord) -> None:
        """Write one record's snapshots; bridge parity gates both on ``outcome == "ok"``
        (findings I2a/I2b). Propagates whatever the write raised so :meth:`write_batch` can count
        it as one record failure — never logged here with payload detail, only by the caller."""
        from examlops.data.drift import write_drift_snapshot, write_input_snapshot

        if record.outcome != "ok":
            return
        if record.prediction is not None:
            write_drift_snapshot(
                record.model, record.alias, float(record.prediction), record.job_id
            )
        if record.embedding is not None and record.embedding.dim > 0:
            write_input_snapshot(
                record.model,
                record.alias,
                record.embedding.norm,
                record.embedding.mean,
                record.embedding.std,
                record.job_id,
            )
            if self._on_baseline is not None:
                _refresh_baseline(record.model, self._on_baseline)

    def close(self) -> None:
        """No resources to release; present to satisfy :class:`TelemetrySink`. Idempotent."""


def _default_queue_max() -> int:
    raw = os.getenv(QUEUE_MAX_ENV, "").strip()
    if not raw:
        return _QUEUE_MAX_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "dataplane telemetry: %s=%r is not an integer; using %d",
            QUEUE_MAX_ENV,
            raw,
            _QUEUE_MAX_DEFAULT,
        )
        return _QUEUE_MAX_DEFAULT
    return max(1, value)


@dataclass
class _Stats:
    dropped: int = 0
    failed: int = 0


class TelemetrySpool:
    """A bounded, drop-on-full queue of :class:`TelemetryRecord`, drained by one daemon thread.

    Bounded because an unbounded buffer turns a slow sink into a memory leak. Drop-on-full is the
    right overflow policy for this data: drift and input-embedding statistics are windowed
    aggregates, so a lost sample shifts nothing an operator acts on, while blocking the caller
    would stall the reply path this seam exists to protect. Every drop and every failed write is
    counted (:meth:`stats`), with optional ``on_drop``/``on_fail`` callbacks — each called once
    per unit counted, no argument, mirroring each other — for a later metrics task to hook (task
    A5 owns ``dataplane_stream_*`` counters).

    The drainer thread starts eagerly at construction (a daemon thread, so it never blocks process
    exit) rather than lazily on first :meth:`offer` — simpler lifecycle, no first-offer race to
    reason about, and the cost of one idle thread per spool is negligible.

    Double counting, deliberately (review P1): if ``close(timeout)`` gives up on a busy drainer it
    counts the still-queued records as **dropped**, and if the drainer's own final flush then
    *fails* on those same records the sink counts them as **failed** too. One record can therefore
    appear in both counters. That is the "over-report loss, never under-report" rule applied twice
    to the same shutdown; the alternative (waiting to see which it was) is exactly the unbounded
    close this bound exists to prevent.

    ``close(timeout)``: stops intake, flushes what is already queued, and closes the sink — but
    the **drainer thread** closes the sink itself, right after its own final flush, never
    ``close()``'s caller. That avoids racing a slow/blocking sink's in-flight write with a close
    call from another thread. If the drainer is still busy past ``timeout``, ``close()`` returns
    anyway (the caller is never blocked indefinitely) and counts whatever is still queued as
    dropped — a conservative count: if the drainer later does finish that flush and successfully
    writes some of those records, they are written *and* already counted as dropped, which biases
    toward over-reporting loss, never under-reporting it.
    """

    def __init__(
        self,
        sink: TelemetrySink,
        *,
        maxsize: int | None = None,
        batch: int = _DEFAULT_BATCH,
        on_drop: Callable[[], None] | None = None,
        on_fail: Callable[[], None] | None = None,
    ) -> None:
        self._sink = sink
        self._maxsize = max(1, maxsize if maxsize is not None else _default_queue_max())
        self._batch = max(1, batch)
        self._queue: queue.Queue[TelemetryRecord] = queue.Queue(maxsize=self._maxsize)
        self._stats = _Stats()
        self._stats_lock = threading.Lock()
        self._on_drop = on_drop
        self._on_fail = on_fail
        self._closed = False
        self._close_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="dataplane-telemetry-spool", daemon=True
        )
        self._thread.start()

    def offer(self, record: TelemetryRecord) -> bool:
        """Enqueue ``record``; never blocks. ``False`` if the spool is closed or the queue is
        full — both count as a drop.

        The closed-check and the enqueue happen under one lock (fix round 1, finding I3): without
        it, ``offer`` could read "not closed", ``close()`` could finish its whole shutdown
        (including the drainer's final drain) on another thread, and only *then* would this
        ``offer`` enqueue — accepted (``True``), yet never written and never counted as dropped.
        Holding the lock is safe here because ``put_nowait`` never blocks, so it is held for a
        handful of instructions, uncontended outside of shutdown.
        """
        with self._close_lock:
            if self._closed:
                refused = True
            else:
                try:
                    self._queue.put_nowait(record)
                    refused = False
                except queue.Full:
                    refused = True
        if refused:
            self._count_drop()
            return False
        return True

    def stats(self) -> dict[str, int]:
        """Point-in-time counters: ``dropped``, ``failed``, and ``queued`` (depth, informational)."""
        with self._stats_lock:
            return {
                "dropped": self._stats.dropped,
                "failed": self._stats.failed,
                "queued": self._queue.qsize(),
            }

    def close(self, timeout: float = 5.0) -> None:
        """Stop accepting new records and flush what is already queued within ``timeout``.
        Idempotent. After this returns, :meth:`offer` always returns ``False`` (counted as a
        drop). See the class docstring for what happens to the sink and to still-queued records
        when the drainer does not finish within ``timeout``.
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            remaining = self._queue.qsize()
            logger.warning(
                "dataplane telemetry: close(timeout=%.1fs) returned while the drainer was still "
                "busy; %d queued record(s) counted as dropped (the drainer will still flush them "
                "and close the sink itself once it finishes)",
                timeout,
                remaining,
            )
            if remaining:
                self._count_drop_n(remaining)

    def _count_drop(self) -> None:
        self._count_drop_n(1)

    def _count_drop_n(self, n: int) -> None:
        with self._stats_lock:
            self._stats.dropped += n
        self._call_hook_n(self._on_drop, n, "on_drop")

    def _count_fail(self, n: int) -> None:
        with self._stats_lock:
            self._stats.failed += n
        self._call_hook_n(self._on_fail, n, "on_fail")

    def _call_hook_n(self, hook: Callable[[], None] | None, n: int, name: str) -> None:
        if hook is None:
            return
        for _ in range(n):
            try:
                hook()
            except Exception:  # noqa: BLE001 - a caller's hook must not break the spool
                logger.warning("dataplane telemetry: %s callback raised", name, exc_info=True)

    def _run(self) -> None:
        poll_s = 0.02
        pending: list[TelemetryRecord] = []
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=poll_s)
            except queue.Empty:
                if pending:
                    self._flush(pending)
                    pending = []
                continue
            pending.append(item)
            self._queue.task_done()
            while len(pending) < self._batch:
                try:
                    pending.append(self._queue.get_nowait())
                    self._queue.task_done()
                except queue.Empty:
                    break
            if len(pending) >= self._batch:
                self._flush(pending)
                pending = []
        # Stop requested: drain whatever is already queued (never block), flush it, and only then
        # close the sink — this thread owns the sink's lifecycle end to end (fix round 1, finding
        # I4), so a slow/blocking write is never raced by close() running on another thread.
        while True:
            try:
                pending.append(self._queue.get_nowait())
                self._queue.task_done()
            except queue.Empty:
                break
        if pending:
            self._flush(pending)
        try:
            self._sink.close()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise out of this thread
            logger.warning("dataplane telemetry: sink close failed: %s", type(exc).__name__)

    def _flush(self, records: list[TelemetryRecord]) -> None:
        try:
            write_batch = getattr(self._sink, "write_batch", None)
            if write_batch is not None:
                write_batch(records)
            else:
                self._write_via_shim(records)
        except Exception as exc:  # noqa: BLE001 - telemetry must never take the caller down
            # A sink following the TelemetryWriteError contract names exactly how many of this
            # batch failed; anything else is treated pessimistically as the whole batch.
            failed = getattr(exc, "failed", len(records))
            self._count_fail(failed)
            logger.warning(
                "dataplane telemetry: sink write failed for %d/%d record(s): %s",
                failed,
                len(records),
                type(exc).__name__,
            )

    def _write_via_shim(self, records: list[TelemetryRecord]) -> None:
        """The ``write(record)``-only shim (finding I6): each record gets its own ``try``, so one
        bad record neither aborts the rest nor gets the successes counted as failed."""
        failed = 0
        for record in records:
            try:
                self._sink.write(record)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - one bad record must not abort the batch
                failed += 1
        if failed:
            raise TelemetryWriteError(failed=failed, total=len(records))


__all__ = [
    "QUEUE_MAX_ENV",
    "DbTelemetrySink",
    "EmbeddingStats",
    "TelemetryRecord",
    "TelemetrySink",
    "TelemetrySpool",
    "TelemetryWriteError",
]
