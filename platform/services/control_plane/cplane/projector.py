"""The serving-snapshot projector: the one place that compiles what serving should do (P4.2).

Runs in every control-plane replica, acts only in the one holding the
``control-plane:serving-snapshot`` lease. Each tick (about a second) it compares the outbox
watermark of the topics that change serving — ``serving.traffic_changed``,
``serving.shadow_changed``, ``model.alias_changed`` — with the last one it compiled for, and
recompiles when it moved. A full recompile also runs every ``interval`` seconds regardless, which
is what picks up a change made behind the platform's back (an alias moved in the MLflow UI).

A failed compile keeps the previous generation in force and backs off exponentially (capped at a
minute), so an MLflow outage costs a stale snapshot and a log line per backoff step — never a
partial snapshot and never a hammered MLflow.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("control-plane")

LEASE_KEY = "control-plane:serving-snapshot"


class SnapshotProjector:
    def __init__(
        self,
        *,
        coordinator: Callable[[], Any],
        holder: str,
        interval: float,
        lease_seconds: float,
        metrics: Any,
        compile_and_publish: Callable[[], tuple[int, bool]] | None = None,
        watermark: Callable[[], int] | None = None,
    ) -> None:
        from examlops import serving_snapshot  # noqa: PLC0415

        self._coordinator = coordinator
        self.holder = holder
        self.interval = interval
        self.lease_seconds = lease_seconds
        self._metrics = metrics
        self._compile = compile_and_publish or serving_snapshot.compile_and_publish
        self._watermark_of = watermark or serving_snapshot.trigger_watermark
        self._watermark: int | None = None
        self._next_full = 0.0
        self._next_retry = 0.0
        self._failures = 0
        self.leader = False
        self.generation: int | None = None
        self.last_ok: float | None = None
        self.last_error: str | None = None

    def step(self) -> str:
        """One tick: ``not_leader`` | ``idle`` | ``backoff`` | ``published`` | ``unchanged`` | ``error``."""
        try:
            self.leader = bool(
                self._coordinator().try_lock(LEASE_KEY, self.holder, self.lease_seconds)
            )
        except Exception as exc:  # noqa: BLE001 - coordination trouble is reported, not raised
            self.leader = False
            self.last_error = f"coordination: {exc}"
            return "not_leader"
        if not self.leader:
            return "not_leader"
        now = time.monotonic()
        if now < self._next_retry:
            return "backoff"
        try:
            watermark = self._watermark_of()
        except Exception as exc:  # noqa: BLE001
            return self._failed(now, f"outbox: {exc}")
        if watermark == self._watermark and now < self._next_full:
            return "idle"
        try:
            generation, published = self._compile()
        except Exception as exc:  # noqa: BLE001 - the previous generation stays in force
            return self._failed(now, str(exc))
        self._watermark = watermark
        self._next_full = now + self.interval
        self._failures = 0
        self.generation = generation
        self.last_ok = time.time()
        self.last_error = None
        self._metrics.set_snapshot(generation, self.last_ok)
        if published:
            logger.info("Serving snapshot generation %d published", generation)
        return "published" if published else "unchanged"

    def _failed(self, now: float, error: str) -> str:
        self._failures += 1
        self._next_retry = now + min(60.0, 2.0 ** min(self._failures, 6))
        if error != self.last_error:
            logger.warning("Serving snapshot compile failed: %s", error)
        self.last_error = error
        self._metrics.record_snapshot_error()
        return "error"

    def release(self) -> None:
        try:
            self._coordinator().unlock(LEASE_KEY, self.holder)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not release the serving-snapshot lease: %s", exc)
        self.leader = False

    def run(self, stop: threading.Event, tick: float) -> None:
        logger.info(
            "Serving snapshot projector started (full recompile every %.0fs)", self.interval
        )
        try:
            while not stop.wait(timeout=tick):
                self.step()
        finally:
            self.release()
            logger.info("Serving snapshot projector stopped")

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "leader": self.leader,
            "generation": self.generation,
            "last_ok_seconds_ago": None
            if self.last_ok is None
            else int(time.time() - self.last_ok),
            "error": self.last_error,
        }
