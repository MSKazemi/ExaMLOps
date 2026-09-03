"""Reusable three-state circuit breaker.

Generalized from the proven control-plane ``_CircuitBreaker``
(``platform/services/control_plane/app.py``). Unlike that version it does not
depend on FastAPI — it raises :class:`CircuitOpenError` instead of ``HTTPException``
so it can wrap MLflow, Ray Serve, MinIO, Zenodo, or any other dependency from any
package. Call sites map :class:`CircuitOpenError` to their own transport
(HTTP 503, an error string, a degraded response, …).

CLOSED → OPEN → HALF-OPEN → CLOSED:
  * Opens after ``fail_max`` consecutive failures.
  * After ``reset_timeout`` seconds in OPEN, allows one trial call (HALF-OPEN).
  * A successful trial closes the breaker; a failed trial re-opens it immediately.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    """Raised by :meth:`CircuitBreaker.call` while the breaker is OPEN."""

    def __init__(self, name: str) -> None:
        super().__init__(f"circuit breaker '{name}' open — upstream unavailable, retry later")
        self.name = name


class CircuitBreaker:
    """Thread-safe circuit breaker.

    Args:
        name: label used in logs and :class:`CircuitOpenError`.
        fail_max: consecutive failures that trip the breaker OPEN.
        reset_timeout: seconds to wait in OPEN before probing (HALF-OPEN).
        is_failure: predicate deciding whether an exception counts as an upstream
            failure. Defaults to "every exception counts". Pass a custom predicate
            to ignore client errors (e.g. treat only HTTP 5xx as failures).
        on_open: optional callback fired once each time the breaker trips OPEN
            (e.g. to increment a Prometheus counter).
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"

    def __init__(
        self,
        name: str = "upstream",
        fail_max: int = 5,
        reset_timeout: float = 30.0,
        is_failure: Callable[[BaseException], bool] | None = None,
        on_open: Callable[[], None] | None = None,
    ) -> None:
        self.name = name
        self._fail_max = fail_max
        self._reset_timeout = reset_timeout
        self._is_failure = is_failure or (lambda _exc: True)
        self._on_open = on_open
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at = 0.0
        # True while the single HALF-OPEN trial call is in flight (C12): without it, every
        # caller arriving after reset_timeout elapsed passed at once — a probe *stampede*
        # against an upstream that just proved itself unhealthy.
        self._probing = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == self.OPEN and (
                time.monotonic() - self._opened_at >= self._reset_timeout
            ):
                return self.HALF_OPEN
            return self._state

    def call(self, fn: Callable[[], T]) -> T:
        """Run ``fn`` under the breaker. Raises :class:`CircuitOpenError` when OPEN."""
        with self._lock:
            if self._state == self.OPEN:
                if time.monotonic() - self._opened_at >= self._reset_timeout:
                    self._state = self.HALF_OPEN
                    self._probing = True
                    logger.info("circuit '%s' HALF-OPEN — probing", self.name)
                else:
                    raise CircuitOpenError(self.name)
            elif self._state == self.HALF_OPEN:
                # Exactly one trial call is admitted; concurrent callers fail fast until the
                # in-flight probe resolves (success → CLOSED, failure → OPEN).
                if self._probing:
                    raise CircuitOpenError(self.name)
                self._probing = True

        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            if self._is_failure(exc):
                self._on_failure()
            else:
                # A non-failure exception (e.g. a client error under a custom predicate) still
                # ends the trial call — release the probe slot so the breaker cannot wedge.
                self._release_probe()
            raise
        else:
            self._on_success()
            return result

    def _release_probe(self) -> None:
        with self._lock:
            self._probing = False

    def _on_success(self) -> None:
        with self._lock:
            if self._state == self.HALF_OPEN:
                logger.info("circuit '%s' CLOSED — upstream recovered", self.name)
            self._state = self.CLOSED
            self._failures = 0
            self._probing = False

    def _on_failure(self) -> None:
        tripped = False
        with self._lock:
            self._probing = False
            self._failures += 1
            if self._failures >= self._fail_max or self._state == self.HALF_OPEN:
                if self._state != self.OPEN:
                    tripped = True
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                logger.error(
                    "circuit '%s' OPENED after %d consecutive failures",
                    self.name,
                    self._failures,
                )
        if tripped and self._on_open is not None:
            try:
                self._on_open()
            except Exception:  # noqa: BLE001 — metrics hook must never break the caller
                logger.debug("circuit '%s' on_open hook failed", self.name, exc_info=True)
