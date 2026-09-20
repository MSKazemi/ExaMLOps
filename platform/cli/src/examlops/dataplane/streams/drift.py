"""Drift aggregator for the dataplane stream ingress (ADR 0130/0131, Plan 2, task A5).

A port of the Dataplane bus bridge's ``DriftTracker`` (``platform/clients/dataplane_bus_bridge.py`` —
read, never imported) to a multi-replica, thread-based ingress:

* **Same signal.** A rolling window of the last ``window`` model outcomes per model; a failure
  rate at or above ``threshold`` over a *full* window trips a retrain. Only genuine model outcomes
  are observed — the ingress feeds ``ok`` (success) and ``model`` (failure) and nothing else, so an
  outage, a validation error, a shed request or a deadline can never masquerade as drift.
* **Same defaults.** Window 50, threshold 0.5, cooldown 300 s — the bridge's ``DRIFT_WINDOW``/
  ``DRIFT_THRESHOLD``/``DRIFT_COOLDOWN`` defaults, here under
  ``EXAMLOPS_DATAPLANE_DRIFT_{WINDOW,THRESHOLD,COOLDOWN_SECONDS}``.
* **Cross-replica cooldown.** The bridge kept its cooldown in process memory, so two bridges
  double-fired. Here it is the coordinator lock ``dataplane:drift-retrain:{model}`` (TTL = the
  cooldown, holder = this replica), so exactly one replica submits per cooldown. A rejected or
  raising trigger releases it, as the bridge's ``_last_retrain.pop`` did, so the next breach
  retries instead of waiting out a cooldown nothing earned. The cooldown is the model's own
  ``drift_auto_retrain.cooldown_s`` when a row exists (R9.4), else the env default — it sets the
  lock TTL, the idempotency bucket and the in-process gate alike.
* **Never on the request thread.** :meth:`DriftAggregator.observe` is O(1) in memory. The DB work
  a trip needs — the ``drift_auto_retrain`` read, the lock, the trigger, the audit row — runs on
  a small bounded executor (:class:`BoundedExecutor`: 2 workers, bounded queue, overflow dropped
  and logged). A per-model in-process gate keeps a sustained breach from queueing one job per
  request. :meth:`DriftAggregator.close` is bounded (``timeout``): queued jobs are cancelled and
  a hung trigger is abandoned, never waited on forever.
* **Sane parameters.** The window is at least 1 and the threshold is clamped into ``(0, 1]``
  with a warning — ``0`` would trip on a window of successes and retrain every cooldown.

Kill switch: a ``drift_auto_retrain`` row with ``enabled`` false suppresses the trigger and
writes a ``retrain_suppressed`` audit row. With **no row**, the bridge retrained anyway (it
hard-coded the dataset); that is kept, logged at WARNING, using the deployment-wide fallback
``EXAMLOPS_DATAPLANE_DRIFT_DATASET`` (a use-case name, so it is configuration, never a literal in
core — ADR 0094). With neither a row nor that fallback there is no dataset to retrain on, and the
trip is suppressed with reason ``no_config``. A suppression keeps the cooldown lock: it is the
fleet's decision for this cooldown window, and one audit row per window beats one per replica.

Audit rows carry the bridge's fields (``reason``, ``error_rate``) plus ``stream`` and
``connector``: ``retrain_suppressed`` and ``retrain_trigger_failed`` are written here; the success
row belongs to the trigger, which knows what it did (:class:`LoggingRetrainTrigger` writes
``dataplane_retrain_would_trigger``; the real ``/v1`` trigger (B1) writes ``retrain_triggered``).
"""

from __future__ import annotations

import logging
import math
import os
import queue
import socket
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from examlops.coordination import Coordinator

logger = logging.getLogger(__name__)

WINDOW_ENV = "EXAMLOPS_DATAPLANE_DRIFT_WINDOW"
THRESHOLD_ENV = "EXAMLOPS_DATAPLANE_DRIFT_THRESHOLD"
COOLDOWN_ENV = "EXAMLOPS_DATAPLANE_DRIFT_COOLDOWN_SECONDS"
#: Dataset to retrain on when a model has no ``drift_auto_retrain`` row (the bridge's hard-coded
#: behaviour, made configuration). Unset ⇒ a model without a row is suppressed (``no_config``).
DATASET_ENV = "EXAMLOPS_DATAPLANE_DRIFT_DATASET"
#: Dataset backend passed to the trigger (the ``drift_auto_retrain`` table has no backend column).
BACKEND_ENV = "EXAMLOPS_DATAPLANE_DRIFT_BACKEND"

#: The bridge's defaults (``DRIFT_WINDOW``/``DRIFT_THRESHOLD``/``DRIFT_COOLDOWN``).
DEFAULT_WINDOW = 50
DEFAULT_THRESHOLD = 0.5
DEFAULT_COOLDOWN_S = 300.0
DEFAULT_BACKEND = "dataplane"

AUDIT_SOURCE = "dataplane"
#: Lock key prefix; the full key is ``dataplane:drift-retrain:{model}``.
LOCK_PREFIX = "dataplane:drift-retrain:"
_LOG_EVERY_S = 60.0


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        logger.warning("dataplane drift: %s=%r is not an integer; using %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        logger.warning("dataplane drift: %s=%r is not a number; using %s", name, raw, default)
        return default


def replica_id() -> str:
    """A holder id unique to this process: ``host:pid:<8 hex>``."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class RetrainTrigger(Protocol):
    """Submits one drift retrain. ``False`` means rejected (the cooldown is then released).

    ``idempotency_key`` is stable for one model within one cooldown window, so a retried or
    doubled submission resolves to one retrain wherever the trigger dedups (the ``/v1`` command
    API does, on ``Idempotency-Key``). An implementation audits its own success.
    """

    def submit(
        self, model: str, dataset: str, backend: str, reason: str, *, idempotency_key: str
    ) -> bool: ...


class LoggingRetrainTrigger:
    """S1's trigger: records that a retrain *would* be submitted, and does nothing else.

    The real ``/v1`` trigger arrives in B1 (and needs ADR 0110 d4 correlation + ``rollback_ref``
    before it may act autonomously). Until then this writes one ``dataplane_retrain_would_trigger``
    audit row — a deliberately different name from ``retrain_triggered``, which would claim a
    retrain that never ran — and accepts.
    """

    ACTION = "dataplane_retrain_would_trigger"

    def submit(
        self, model: str, dataset: str, backend: str, reason: str, *, idempotency_key: str
    ) -> bool:
        from examlops.data.audit import write_audit_event

        logger.warning(
            "dataplane drift: retrain WOULD be submitted for %s (dataset=%s backend=%s) — "
            "LoggingRetrainTrigger is a dry run",
            model,
            dataset,
            backend,
        )
        write_audit_event(
            AUDIT_SOURCE,
            None,
            self.ACTION,
            model,
            {
                "reason": reason,
                "dataset": dataset,
                "backend": backend,
                "idempotency_key": idempotency_key,
            },
        )
        return True


class TaskRunner(Protocol):
    """Runs a job off the caller's thread. ``submit`` never blocks; ``False`` = not accepted.

    ``shutdown(timeout)`` stops intake, cancels queued jobs, and waits at most ``timeout``
    seconds for running ones — it is bounded, so a hung job can never hold a drain up.
    """

    def submit(self, fn: Callable[[], None]) -> bool: ...

    def shutdown(self, timeout: float = 5.0) -> None: ...


class BoundedExecutor:
    """A small pool of **daemon** worker threads (``max_workers``) with a bounded queue
    (``max_pending`` jobs accepted at once, running or queued). A job past the bound is refused,
    never queued without limit.

    Daemon threads are the point (review P3). ``ThreadPoolExecutor``'s workers are deliberately
    *non*-daemon and it registers an interpreter-exit hook that **joins them without a timeout**,
    which runs before any ``atexit`` handler we could register — so one hung retrain trigger would
    hang process exit, and nothing we could add would bound it. Here, exit never waits: a job
    still running at interpreter exit is abandoned, exactly as the drain's own bounded
    :meth:`shutdown` abandons it a moment earlier. Nothing in a trigger job needs an orderly exit
    — it writes through helpers that own their own connection, and its work is idempotent.
    """

    def __init__(self, max_workers: int = 2, max_pending: int = 8) -> None:
        self._max_workers = max(1, max_workers)
        self._slots = threading.BoundedSemaphore(max(1, max_pending))
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, fn: Callable[[], None]) -> bool:
        with self._lock:
            if self._closed:
                return False
        if not self._slots.acquire(blocking=False):
            return False
        with self._lock:
            if self._closed:  # closed while we took a slot
                self._slots.release()
                return False
            self._queue.put(fn)
            if len(self._threads) < self._max_workers:
                thread = threading.Thread(
                    target=self._work,
                    name=f"dataplane-drift-{len(self._threads)}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()
        return True

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:  # the shutdown sentinel: this worker is done
                return
            try:
                job()
            except Exception:  # noqa: BLE001 - a failing job must not take its worker down
                logger.warning("dataplane drift: a queued job failed", exc_info=True)
            finally:
                self._slots.release()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop accepting work, drop what is still queued, and wait at most ``timeout`` seconds
        in total for the jobs already running. Idempotent."""
        with self._lock:
            self._closed = True
            threads = list(self._threads)
        while True:  # cancel what has not started
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            else:
                self._slots.release()
        for _ in threads:
            self._queue.put(None)
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))


@dataclass
class _Window:
    """The last ``window`` outcomes of one model, with a running failure count (O(1) updates)."""

    size: int
    outcomes: deque[bool] = field(default_factory=deque)
    failures: int = 0

    def add(self, failed: bool) -> None:
        if len(self.outcomes) >= self.size:
            if self.outcomes.popleft():
                self.failures -= 1
        self.outcomes.append(failed)
        if failed:
            self.failures += 1

    @property
    def full(self) -> bool:
        return len(self.outcomes) >= self.size

    @property
    def rate(self) -> float:
        return self.failures / len(self.outcomes) if self.outcomes else 0.0


@dataclass(frozen=True)
class _Trip:
    model: str
    rate: float
    at: float
    stream: str | None
    connector: str | None


@dataclass(frozen=True)
class _Config:
    """What ``drift_auto_retrain`` says for one model: the dataset (or why not) and the cooldown."""

    dataset: str | None
    suppressed: str | None
    cooldown_s: float


def _clamp_window(window: int) -> int:
    if window < 1:
        logger.warning("dataplane drift: window %d is below 1; using 1", window)
        return 1
    return window


def _clamp_threshold(threshold: float, window: int) -> float:
    """Keep the threshold in ``(0, 1]``: ``0`` or less would trip on a window of successes (a
    retrain every cooldown), above ``1`` could never trip."""
    if math.isnan(threshold):
        logger.warning("dataplane drift: threshold is NaN; using the default %s", DEFAULT_THRESHOLD)
        return DEFAULT_THRESHOLD
    if threshold <= 0:
        floor = 1.0 / window  # the smallest rate a full window can show: one failure
        logger.warning(
            "dataplane drift: threshold %s is not above 0; clamped to %s", threshold, floor
        )
        return floor
    if threshold > 1:
        logger.warning("dataplane drift: threshold %s is above 1; clamped to 1.0", threshold)
        return 1.0
    return threshold


class DriftAggregator:
    """Rolling per-model model-failure rate → a cross-replica, cooldown-gated retrain trigger.

    ``cooldown_s`` is the default cooldown; a model whose ``drift_auto_retrain`` row carries a
    positive ``cooldown_s`` uses that instead, for its lock TTL, its idempotency bucket and its
    in-process gate (R9.4 — the operator's per-model setting is honoured).
    """

    def __init__(
        self,
        trigger: RetrainTrigger,
        coord: Coordinator,
        cooldown_s: float | None = None,
        window: int | None = None,
        threshold: float | None = None,
        now: Callable[[], float] = time.time,
        *,
        executor: TaskRunner | None = None,
        holder: str | None = None,
        default_dataset: str | None = None,
        backend: str | None = None,
    ) -> None:
        self._trigger = trigger
        self._coord = coord
        cooldown = cooldown_s if cooldown_s is not None else _env_float(COOLDOWN_ENV, 0.0)
        self._cooldown = cooldown if cooldown > 0 else DEFAULT_COOLDOWN_S
        size = window if window is not None else _env_int(WINDOW_ENV, DEFAULT_WINDOW)
        self._window = _clamp_window(size)
        raw_threshold = (
            threshold if threshold is not None else _env_float(THRESHOLD_ENV, DEFAULT_THRESHOLD)
        )
        self._threshold = _clamp_threshold(raw_threshold, self._window)
        self._now = now
        self._executor: TaskRunner = executor if executor is not None else BoundedExecutor()
        self._holder = holder or replica_id()
        fallback = default_dataset if default_dataset is not None else os.getenv(DATASET_ENV, "")
        self._default_dataset = fallback.strip() or None
        self._backend = (backend or os.getenv(BACKEND_ENV, "").strip()) or DEFAULT_BACKEND
        self._lock = threading.Lock()
        self._windows: dict[str, _Window] = {}
        # Per-model in-process gate: no new job before this time, and one job in flight at most.
        self._next_eligible: dict[str, float] = {}
        self._pending: set[str] = set()
        self._counts = {
            "trips": 0,
            "submitted": 0,
            "rejected": 0,
            "suppressed": 0,
            "contended": 0,
            "dropped": 0,
        }
        self._last_log: dict[str, float] = {}
        self._closed = False

    @property
    def cooldown_s(self) -> float:
        return self._cooldown

    @property
    def window(self) -> int:
        return self._window

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def holder(self) -> str:
        return self._holder

    def observe(
        self,
        model: str,
        failed: bool,
        *,
        stream: str | None = None,
        connector: str | None = None,
    ) -> None:
        """Record one model outcome. O(1), in memory, never raises, never blocks on I/O."""
        if self._closed:
            self._log_throttled("closed", "dataplane drift: aggregator closed; observation ignored")
            return
        try:
            self._observe(model, failed, stream, connector)
        except Exception:  # noqa: BLE001 - drift must never break a request
            self._log_throttled("observe", "dataplane drift: observe failed", exc_info=True)

    def _observe(self, model: str, failed: bool, stream: str | None, connector: str | None) -> None:
        with self._lock:
            win = self._windows.get(model)
            if win is None:
                win = self._windows[model] = _Window(self._window)
            win.add(bool(failed))
            if not (win.full and win.rate >= self._threshold):
                return
            now = self._now()
            if model in self._pending or now < self._next_eligible.get(model, float("-inf")):
                return
            rate = win.rate
            self._pending.add(model)
            self._next_eligible[model] = now + self._cooldown  # provisional, until the job knows
            self._counts["trips"] += 1
        trip = _Trip(model=model, rate=rate, at=now, stream=stream, connector=connector)
        accepted = False
        try:
            accepted = self._executor.submit(lambda: self._run(trip))
        finally:
            if not accepted:
                with self._lock:
                    self._pending.discard(model)
                    self._next_eligible.pop(model, None)
                    self._counts["dropped"] += 1
                if self._closed:
                    self._log_throttled(
                        "closed", "dataplane drift: aggregator closed; observation ignored"
                    )
                else:
                    self._log_throttled(
                        "dropped",
                        "dataplane drift: retrain job for %s dropped (executor full)",
                        model,
                    )

    def _run(self, trip: _Trip) -> None:
        """The off-thread half of a trip: kill switch → lock → trigger → audit. Never raises."""
        hold: float | None = None
        try:
            hold = self._decide(trip)
        except Exception:  # noqa: BLE001 - a background job must not die silently
            logger.warning(
                "dataplane drift: retrain decision for %s failed", trip.model, exc_info=True
            )
        finally:
            with self._lock:
                self._pending.discard(trip.model)
                if hold is None:
                    self._next_eligible.pop(trip.model, None)
                else:
                    self._next_eligible[trip.model] = trip.at + hold

    def _decide(self, trip: _Trip) -> float | None:
        """Returns the cooldown the in-process gate should stay shut for, or ``None`` to reopen it
        now (so the next breach retries)."""
        try:
            config = self._config(trip.model)
        except Exception as exc:  # noqa: BLE001 - an unreadable kill switch must not retrain
            self._log_throttled(
                "config",
                "dataplane drift: drift_auto_retrain unreadable for %s (%s); not retraining",
                trip.model,
                type(exc).__name__,
            )
            return None
        cooldown = config.cooldown_s

        key = LOCK_PREFIX + trip.model
        try:
            locked = self._coord.try_lock(key, self._holder, cooldown)
            held = locked
        except Exception as exc:  # noqa: BLE001 - fail open; the idempotency key still dedups
            self._log_throttled(
                "lock",
                "dataplane drift: cooldown lock unavailable (%s); proceeding without it",
                type(exc).__name__,
            )
            locked, held = True, False
        if not locked:
            with self._lock:
                self._counts["contended"] += 1
            return cooldown  # another replica holds this model's cooldown

        if config.suppressed is not None or config.dataset is None:
            why = config.suppressed or "no_config"
            self._audit(
                "retrain_suppressed", trip, {"suppressed_reason": why, "cooldown_s": cooldown}
            )
            with self._lock:
                self._counts["suppressed"] += 1
            logger.warning("dataplane drift: retrain for %s suppressed (%s)", trip.model, why)
            return cooldown  # the fleet's decision for this window: the lock is kept

        idempotency_key = f"dataplane:drift:{trip.model}:{int(trip.at // cooldown)}"
        logger.warning(
            "dataplane drift: drift detected for %s (error_rate=%.0f%%) — submitting retrain",
            trip.model,
            trip.rate * 100,
        )
        error: str | None = None
        try:
            accepted = bool(
                self._trigger.submit(
                    trip.model,
                    config.dataset,
                    self._backend,
                    "drift",
                    idempotency_key=idempotency_key,
                )
            )
        except Exception as exc:  # noqa: BLE001 - a raising trigger is a rejected trigger
            accepted, error = False, type(exc).__name__
        if accepted:
            with self._lock:
                self._counts["submitted"] += 1
            return cooldown
        self._unlock(key, held)
        with self._lock:
            self._counts["rejected"] += 1
        logger.error("dataplane drift: retrain trigger REJECTED for %s", trip.model)
        details: dict[str, Any] = {
            "dataset": config.dataset,
            "backend": self._backend,
            "cooldown_s": cooldown,
        }
        if error is not None:
            details["error"] = error
        self._audit("retrain_trigger_failed", trip, details)
        return None

    def _config(self, model: str) -> _Config:
        """The dataset (or suppression reason) and cooldown for ``model``, from
        ``drift_auto_retrain``; with no row, the fallback dataset and the default cooldown."""
        from examlops.data.drift import get_drift_auto_retrain

        row = get_drift_auto_retrain(model)
        if row is None:
            if self._default_dataset is None:
                return _Config(None, "no_config", self._cooldown)
            logger.warning(
                "dataplane drift: no drift_auto_retrain row for %s; retraining on the fallback "
                "dataset %s (%s)",
                model,
                self._default_dataset,
                DATASET_ENV,
            )
            return _Config(self._default_dataset, None, self._cooldown)
        cooldown = self._row_cooldown(row.get("cooldown_s"))
        if not row.get("enabled"):
            return _Config(None, "disabled", cooldown)
        dataset = str(row.get("dataset_name") or "").strip() or self._default_dataset
        if dataset is None:
            return _Config(None, "no_dataset", cooldown)
        return _Config(dataset, None, cooldown)

    def _row_cooldown(self, value: Any) -> float:
        try:
            cooldown = float(value)
        except (TypeError, ValueError):
            return self._cooldown
        return cooldown if math.isfinite(cooldown) and cooldown > 0 else self._cooldown

    def _unlock(self, key: str, held: bool) -> None:
        if not held:
            return
        try:
            self._coord.unlock(key, self._holder)
        except Exception as exc:  # noqa: BLE001 - the TTL frees it anyway
            self._log_throttled(
                "unlock", "dataplane drift: cooldown unlock failed (%s)", type(exc).__name__
            )

    def _audit(self, action: str, trip: _Trip, extra: dict[str, Any]) -> None:
        details: dict[str, Any] = {
            "reason": "drift",
            "error_rate": round(trip.rate, 4),
            "window": self._window,
            "threshold": self._threshold,
            "stream": trip.stream,
            "connector": trip.connector,
            **extra,
        }
        try:
            from examlops.data.audit import write_audit_event

            write_audit_event(AUDIT_SOURCE, None, action, trip.model, details)
        except Exception as exc:  # noqa: BLE001 - an audit failure must not kill the job
            logger.warning(
                "dataplane drift: audit %s for %s failed (%s)",
                action,
                trip.model,
                type(exc).__name__,
            )

    def _log_throttled(self, key: str, msg: str, *args: Any, exc_info: bool = False) -> None:
        """Log at most once per :data:`_LOG_EVERY_S` per ``key`` — one noisy condition can never
        hide a different one."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_log.get(key, float("-inf")) < _LOG_EVERY_S:
                return
            self._last_log[key] = now
        logger.warning(msg, *args, exc_info=exc_info)

    def stats(self) -> dict[str, Any]:
        """Counters plus each model's current window fill and failure rate."""
        with self._lock:
            return {
                **self._counts,
                "closed": self._closed,
                "models": {
                    m: {
                        "observed": len(w.outcomes),
                        "failures": w.failures,
                        "error_rate": round(w.rate, 4),
                    }
                    for m, w in self._windows.items()
                },
            }

    def close(self, timeout: float = 5.0) -> None:
        """Stop taking observations, cancel queued jobs, and wait at most ``timeout`` seconds for
        running ones. Bounded: a hung trigger cannot hold a drain up. Idempotent."""
        self._closed = True
        self._executor.shutdown(timeout=timeout)


__all__ = [
    "BACKEND_ENV",
    "COOLDOWN_ENV",
    "DATASET_ENV",
    "DEFAULT_BACKEND",
    "DEFAULT_COOLDOWN_S",
    "DEFAULT_THRESHOLD",
    "DEFAULT_WINDOW",
    "LOCK_PREFIX",
    "THRESHOLD_ENV",
    "WINDOW_ENV",
    "BoundedExecutor",
    "DriftAggregator",
    "LoggingRetrainTrigger",
    "RetrainTrigger",
    "TaskRunner",
    "replica_id",
]
