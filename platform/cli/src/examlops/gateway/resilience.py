"""Resilience primitives (ADR 0153): circuit breaker, retry budget, bulkhead, deadline.

Every time-dependent piece takes an injectable ``clock`` so cool-downs and windows are tested
exactly. None of them knows about providers: they are policy over outcomes the router reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections import deque
from collections.abc import AsyncIterator, Callable

from examlops.gateway.providers.base import ProviderError

Clock = Callable[[], float]


class CircuitBreaker:
    """closed → open → half_open → closed, per (provider, upstream model) (ADR 0153 d3).

    Only outcomes the caller classifies as *retryable upstream faults* are reported as failures;
    a client mistake (400, unknown model) must never trip it. The cool-down grows as
    ``base·2^k`` up to ``cap``, with ±``jitter`` so replicas do not probe in lock-step.
    """

    def __init__(
        self,
        *,
        clock: Clock = time.monotonic,
        rng: random.Random | None = None,
        fail_threshold: int = 5,
        error_ratio: float = 0.5,
        window_s: float = 30.0,
        min_samples: int = 10,
        base_open_s: float = 5.0,
        cap_open_s: float = 60.0,
        half_open_trials: int = 2,
        jitter: float = 0.2,
    ) -> None:
        self._clock = clock
        self._rng = rng or random.Random()
        self.fail_threshold = fail_threshold
        self.error_ratio = error_ratio
        self.window_s = window_s
        self.min_samples = min_samples
        self.base_open_s = base_open_s
        self.cap_open_s = cap_open_s
        self.half_open_trials = half_open_trials
        self.jitter = jitter
        self._state = "closed"
        self._events: deque[tuple[float, bool]] = deque()
        self._consecutive = 0
        self._k = 0
        self._open_until = 0.0
        self._trials_inflight = 0
        self._trial_successes = 0

    # ── state ────────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        if self._state == "open" and self._clock() >= self._open_until:
            self._state = "half_open"
            self._trials_inflight = 0
            self._trial_successes = 0

    @property
    def state(self) -> str:
        self._tick()
        return self._state

    def open_remaining(self) -> float:
        return max(0.0, self._open_until - self._clock()) if self.state == "open" else 0.0

    def would_allow(self) -> bool:
        """Non-consuming: could a request be admitted right now? For candidate filtering."""
        self._tick()
        if self._state == "closed":
            return True
        return self._state == "half_open" and self._trials_inflight < self.half_open_trials

    def allow(self) -> bool:
        """Admit one request (consumes a trial slot while half-open)."""
        self._tick()
        if self._state == "closed":
            return True
        if self._state == "half_open" and self._trials_inflight < self.half_open_trials:
            self._trials_inflight += 1
            return True
        return False

    # ── outcomes ─────────────────────────────────────────────────────────────

    def _record(self, ok: bool) -> None:
        now = self._clock()
        self._events.append((now, ok))
        while self._events and self._events[0][0] < now - self.window_s:
            self._events.popleft()

    def record_success(self) -> None:
        self._tick()
        if self._state == "closed":
            self._consecutive = 0
            self._record(True)
        elif self._state == "half_open":
            self._trials_inflight = max(0, self._trials_inflight - 1)
            self._trial_successes += 1
            if self._trial_successes >= 2:
                self._state, self._k, self._consecutive = "closed", 0, 0
                self._events.clear()
        # open: a late result from before the trip carries no information — ignored

    def record_failure(self) -> None:
        self._tick()
        if self._state == "closed":
            self._consecutive += 1
            self._record(False)
            fails = sum(1 for _, ok in self._events if not ok)
            n = len(self._events)
            if self._consecutive >= self.fail_threshold or (
                n >= self.min_samples and fails / n >= self.error_ratio
            ):
                self._trip()
        elif self._state == "half_open":
            self._trials_inflight = max(0, self._trials_inflight - 1)
            self._trip()

    def _trip(self) -> None:
        base = min(self.cap_open_s, self.base_open_s * (2**self._k))
        spread = 1.0 + self.jitter * (self._rng.random() * 2.0 - 1.0)
        self._open_until = self._clock() + base * spread
        self._state = "open"
        self._k += 1
        self._consecutive = 0
        self._events.clear()


class RetryBudget:
    """Retries may be at most ``ratio`` of recent requests, with a small floor (ADR 0153 d5).

    This is what stops one slow upstream turning into a retry storm: when every request fails
    and asks to retry, only a bounded share is granted and the rest fail fast.
    """

    def __init__(
        self,
        *,
        clock: Clock = time.monotonic,
        ratio: float = 0.2,
        window_s: float = 10.0,
        min_retries: int = 3,
    ) -> None:
        self._clock = clock
        self.ratio = ratio
        self.window_s = window_s
        self.min_retries = min_retries
        self._requests: deque[float] = deque()
        self._retries: deque[float] = deque()

    def _prune(self) -> None:
        cutoff = self._clock() - self.window_s
        for q in (self._requests, self._retries):
            while q and q[0] < cutoff:
                q.popleft()

    def note_request(self) -> None:
        self._requests.append(self._clock())

    def try_retry(self) -> bool:
        self._prune()
        allowed = max(self.min_retries, int(self.ratio * len(self._requests)))
        if len(self._retries) >= allowed:
            return False
        self._retries.append(self._clock())
        return True


class Bulkhead:
    """Bounded concurrency with a bounded, time-limited queue (ADR 0153 d6).

    On a single-GPU Ollama, letting requests pile up only converts them into client-side
    timeouts. Full ⇒ ``queue_full`` immediately; waiting too long ⇒ ``queue_full`` with a
    ``retry_after`` hint. Neither counts against the upstream's health.
    """

    def __init__(
        self, max_concurrency: int, max_queue: int = 8, queue_timeout: float = 5.0
    ) -> None:
        self.max_concurrency = max_concurrency
        self.max_queue = max_queue
        self.queue_timeout = queue_timeout
        self._sem = asyncio.Semaphore(max_concurrency)
        self.inflight = 0
        self.waiting = 0

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._sem.locked():
            if self.waiting >= self.max_queue:
                raise ProviderError(
                    "queue_full", "too many requests queued for this backend", retry_after=1.0
                )
            self.waiting += 1
            try:
                await asyncio.wait_for(self._sem.acquire(), self.queue_timeout)
            except TimeoutError:
                raise ProviderError(
                    "queue_full",
                    f"waited {self.queue_timeout:g}s for a free slot",
                    retry_after=max(1.0, self.queue_timeout),
                ) from None
            finally:
                self.waiting -= 1
        else:
            await self._sem.acquire()
        self.inflight += 1
        try:
            yield
        finally:
            self.inflight -= 1
            self._sem.release()


class Deadline:
    """The time one request may still spend, shared by every attempt (ADR 0153 d5)."""

    def __init__(self, total_s: float, *, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._end = clock() + total_s

    @classmethod
    def from_budget(
        cls, route_total_s: float, *, caller_ms: float | None, clock: Clock = time.monotonic
    ) -> Deadline:
        """The tighter of the route's total and the caller's ``X-ExaMLOps-Budget-Ms``."""
        total = route_total_s if caller_ms is None else min(route_total_s, caller_ms / 1000.0)
        return cls(total, clock=clock)

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0
