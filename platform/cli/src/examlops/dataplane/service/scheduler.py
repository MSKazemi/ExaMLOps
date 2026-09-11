"""Interval parsing for source schedules (``15m``, ``6h``, ``1d``, ``@hourly``, ``@daily``).

Also the :class:`Scheduler` the dataplane service runs (ADR 0130 §9): it pulls each enabled source
whose schedule is due, and it queues the pulls the HTTP API accepts. Stdlib only at import time —
``examlops.dataplane.pull`` imports :func:`parse_interval` from here, so every platform import
stays inside the methods.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from examlops.dataplane.types import DataplaneError, SpecError

if TYPE_CHECKING:  # pragma: no cover
    from examlops.dataplane.pull import SourceDef

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_ALIASES = {"@hourly": 3600, "@daily": 86400, "@weekly": 7 * 86400}


def parse_interval(value: str) -> int:
    v = value.strip().lower()
    if v in _ALIASES:
        return _ALIASES[v]
    m = re.fullmatch(r"(\d+)([smhd])", v)
    if not m or int(m.group(1)) == 0:
        raise SpecError(f"schedule {value!r}: use e.g. 15m, 6h, 1d, @hourly, @daily")
    seconds = int(m.group(1)) * _UNITS[m.group(2)]
    if seconds < 60:
        raise SpecError("schedule must be at least 1m")
    return seconds


logger = logging.getLogger(__name__)

# Outcomes of pulls that ended without a catalog row (failed before run_pull recorded them).
_MAX_OUTCOMES = 1000
# The fields of a pending pull a caller may see (the lock key and source key stay internal).
_VIEW_KEYS: tuple[str, ...] = ("id", "project", "source", "trigger_kind")


def parse_timestamp(value: Any) -> float | None:
    """Epoch seconds for a catalog timestamp, or ``None`` when absent or unreadable.

    ``CURRENT_TIMESTAMP`` is UTC with no zone — ``YYYY-MM-DD HH:MM:SS`` on SQLite, possibly a
    ``datetime`` on Postgres — so a naive value is read as UTC; reading it as local time would
    skew every schedule and freshness gauge on a host that is not on UTC.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace(" ", "T", 1))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _safe_error(exc: BaseException) -> str:
    from examlops.dataplane.safety import redact

    if isinstance(exc, DataplaneError):
        return redact(str(exc))  # run_pull's messages are already "<Type>: <redacted text>"
    return redact(f"{type(exc).__name__}: {exc}")


class Scheduler:
    """Every ``interval_s`` seconds, pull each enabled source whose schedule is due.

    Every pull — scheduled, or accepted by the HTTP API — goes through ``run_pull``. Before it is
    queued, :meth:`submit` *reserves* it by taking ``run_pull``'s own per-source coordinator lock
    with the pull id as holder (``run_pull`` re-enters a lock its holder already owns). So a pull
    that is queued but not yet started already blocks a twin from this process, the CLI or a
    second replica, and a stale ``running`` row left by a crashed process blocks nothing once
    its lock has expired.
    """

    def __init__(self, interval_s: float = 30.0, workers: int = 2) -> None:
        if interval_s <= 0:
            raise ValueError(f"scheduler interval must be > 0 seconds, not {interval_s}")
        if workers < 1:
            raise ValueError(f"scheduler needs at least one worker, not {workers}")
        self.interval_s = interval_s
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dataplane-pull")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mu = threading.Lock()
        # pull_id -> {id, project, source, trigger_kind, key, lock}: reserved, not yet finished.
        self._pending: dict[str, dict[str, Any]] = {}
        self._outcomes: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # source key -> when the scheduler last tried it, so a source that fails before a pull
        # row exists (connector not installed, say) is retried once per interval, not per tick.
        self._attempted: dict[str, float] = {}
        self._futures: set[Future[None]] = set()

    # ── what is due ──────────────────────────────────────────────────────────

    def due_sources(self, now: float) -> list[SourceDef]:
        from examlops.data import dataplane as catalog
        from examlops.dataplane.pull import list_source_defs

        with self._mu:
            busy = {p["key"] for p in self._pending.values()}
            attempted = dict(self._attempted)
        due: list[SourceDef] = []
        for src in list_source_defs():
            if not src.enabled or not src.schedule or src.key in busy:
                continue
            try:
                interval = parse_interval(src.schedule)
            except SpecError:
                logger.warning("dataplane: source %s has an unreadable schedule; skipped", src.key)
                continue
            tried = attempted.get(src.key)
            if tried is not None and now - tried < interval:
                continue
            last = catalog.last_pull(src.project, src.name, committed_only=False)
            started = parse_timestamp(last["started_at"]) if last else None
            if started is None or now - started >= interval:
                due.append(src)
        return due

    def tick(self, now: float | None = None) -> list[str]:
        """Queue every due source once; the ids of the pulls actually queued.

        Also reaps interrupted pulls first (task 22a) — cheap (one query) when nothing is stuck,
        and it is what turns a row a crashed process left `running` back into `failed` without
        waiting for the whole service to restart.
        """
        from examlops.dataplane.pull import reap_interrupted_pulls

        try:
            reap_interrupted_pulls()
        except Exception as exc:  # noqa: BLE001 — a datastore hiccup must not stop scheduling
            logger.warning("dataplane: could not reap interrupted pulls: %s", _safe_error(exc))
        now = time.time() if now is None else now
        queued: list[str] = []
        for src in self.due_sources(now):
            with self._mu:
                self._attempted[src.key] = now
            try:
                pull_id = self.submit(src.name, src.project, trigger_kind="schedule")
            except Exception as exc:  # noqa: BLE001 — one bad source must not stop the others
                logger.warning("dataplane: could not schedule %s: %s", src.key, _safe_error(exc))
                continue
            if pull_id:
                queued.append(pull_id)
        return queued

    # ── queueing ─────────────────────────────────────────────────────────────

    def submit(
        self,
        name: str,
        project: str = "",
        *,
        trigger_kind: str,
        pull_id: str | None = None,
        full: bool = False,
        actor: str | None = None,
    ) -> str | None:
        """Reserve and queue one pull. The pull id, or ``None`` when a pull of the source is
        already pending or running anywhere. Raises ``SpecError`` for an unknown source."""
        from examlops.coordination import get_coordinator
        from examlops.data import dataplane as catalog
        from examlops.dataplane.pull import _lease_ttl_s, get_source_def, pull_lock_key
        from examlops.dataplane.types import global_limits

        src = get_source_def(name, project)
        pull_id = pull_id or catalog.new_pull_id()
        lock = pull_lock_key(src.key)
        # The same lease run_pull re-enters and then keeps renewed while the pull runs.
        ttl = _lease_ttl_s(src.limits.capped(global_limits()))
        with self._mu:
            if any(p["key"] == src.key for p in self._pending.values()):
                return None
        # Outside the mutex: the lock is atomic across holders on its own, so two racing submits
        # here cannot both win it, and a slow datastore does not stall status lookups.
        if not get_coordinator().try_lock(lock, pull_id, ttl_s=ttl):
            return None
        with self._mu:
            self._pending[pull_id] = {
                "id": pull_id,
                "project": project,
                "source": name,
                "trigger_kind": trigger_kind,
                "key": src.key,
                "lock": lock,
            }
        try:
            future = self.pool.submit(self._run, pull_id, name, project, trigger_kind, full, actor)
        except RuntimeError:  # the pool is shut down: give the reservation back
            self._finish(pull_id)
            return None
        with self._mu:
            self._futures.add(future)
        future.add_done_callback(lambda f: self._done(f, pull_id))
        return pull_id

    def _done(self, future: Future[None], pull_id: str) -> None:
        with self._mu:
            self._futures.discard(future)
        if future.cancelled():  # dropped by stop() before it ran: release its reservation
            self._finish(pull_id)

    def _run(
        self,
        pull_id: str,
        name: str,
        project: str,
        trigger_kind: str,
        full: bool,
        actor: str | None,
    ) -> None:
        from examlops.dataplane.pull import run_pull

        try:
            run_pull(
                name,
                project=project,
                trigger_kind=trigger_kind,
                pull_id=pull_id,
                full=full,
                actor=actor,
            )
        except Exception as exc:  # noqa: BLE001 — recorded below, never raised into the pool
            self._record_failure(pull_id, exc)
        finally:
            self._finish(pull_id)

    def _record_failure(self, pull_id: str, exc: BaseException) -> None:
        """A pull that failed after ``run_pull`` recorded it is in the catalog; one that failed
        before (source removed or disabled, connector missing, lock lost) is kept here."""
        from examlops.data import dataplane as catalog

        message = _safe_error(exc)
        with self._mu:
            entry = dict(self._pending.get(pull_id) or {"id": pull_id})
        logger.warning(
            "dataplane: pull %s of %s failed: %s", pull_id, entry.get("key", "?"), message
        )
        try:
            if catalog.get_pull(pull_id) is not None:
                return
        except Exception:  # noqa: BLE001 — the catalog being down is exactly when to keep it here
            pass
        view: dict[str, Any] = {k: entry[k] for k in _VIEW_KEYS if k in entry}
        with self._mu:
            self._outcomes[pull_id] = {**view, "status": "failed", "error": message}
            while len(self._outcomes) > _MAX_OUTCOMES:
                self._outcomes.popitem(last=False)

    def _finish(self, pull_id: str) -> None:
        from examlops.coordination import get_coordinator

        with self._mu:
            entry = self._pending.pop(pull_id, None)
        if entry is None:
            return
        try:
            # A no-op when run_pull already released it; frees the reservation when the pull
            # never reached run_pull's own lock.
            get_coordinator().unlock(entry["lock"], pull_id)
        except Exception as exc:  # noqa: BLE001 — the lock's TTL frees it anyway
            logger.warning("dataplane: could not release %s: %s", entry["key"], _safe_error(exc))

    def status_of(self, pull_id: str) -> dict[str, Any] | None:
        """What this process knows about a pull that has no catalog row: ``queued``, or
        ``failed`` before it started. ``None`` when it knows nothing."""
        with self._mu:
            if pull_id in self._outcomes:
                return dict(self._outcomes[pull_id])
            entry = self._pending.get(pull_id)
            if entry is not None:
                view: dict[str, Any] = {k: entry[k] for k in _VIEW_KEYS}
                return {**view, "status": "queued"}
        return None

    def pending(self) -> list[str]:
        with self._mu:
            return list(self._pending)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — a datastore outage must not end the loop
                logger.warning("dataplane: scheduler tick failed: %s", _safe_error(exc))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="dataplane-scheduler", daemon=True)
        self._thread.start()

    def stop(self, wait_s: float = 10.0) -> None:
        """Stop scheduling and drop queued pulls (their reservations are released), then give
        running pulls up to ``wait_s`` seconds to finish. One still running after that keeps
        going on its own thread; its lock and catalog row are its own either way."""
        self._stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        with self._mu:
            running = list(self._futures)
        if running and wait_s > 0:
            futures_wait(running, timeout=wait_s)


__all__ = ["Scheduler", "parse_interval", "parse_timestamp"]
