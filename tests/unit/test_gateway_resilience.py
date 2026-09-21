"""Resilience primitives (ADR 0153): breaker, retry budget, bulkhead, deadline.

All time is a fake clock, so cool-downs and windows are exact rather than slept through.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from examlops.gateway.providers import ProviderError
from examlops.gateway.resilience import Bulkhead, CircuitBreaker, Deadline, RetryBudget


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def breaker(clock: Clock, **kw) -> CircuitBreaker:
    kw.setdefault("jitter", 0.0)  # exact cool-downs unless a test is about jitter
    return CircuitBreaker(clock=clock, **kw)


# ── circuit breaker ───────────────────────────────────────────────────────────


def test_opens_after_consecutive_failures_and_refuses_requests():
    c = Clock()
    b = breaker(c, fail_threshold=3)
    for _ in range(2):
        assert b.allow()
        b.record_failure()
    assert b.state == "closed"
    b.allow()
    b.record_failure()
    assert b.state == "open"
    assert b.allow() is False


def test_a_success_resets_the_consecutive_count():
    b = breaker(Clock(), fail_threshold=3, min_samples=1000)  # ratio rule out of the way
    for _ in range(2):
        b.record_failure()
    b.record_success()
    for _ in range(2):
        b.record_failure()
    assert b.state == "closed"


def test_opens_on_error_ratio_within_the_window():
    c = Clock()
    b = breaker(c, fail_threshold=1000, error_ratio=0.5, min_samples=10, window_s=30)
    for i in range(10):
        # ends on a failure: the ratio is evaluated when a failure is recorded
        (b.record_success if i % 2 == 0 else b.record_failure)()
    assert b.state == "open"


def test_old_samples_leave_the_window():
    c = Clock()
    b = breaker(c, fail_threshold=1000, error_ratio=0.5, min_samples=10, window_s=30)
    for _ in range(9):
        b.record_failure()
    c.advance(31)  # those nine are now outside the window
    b.record_failure()
    assert b.state == "closed"


def test_half_open_after_the_cooldown_admits_a_bounded_number_of_trials():
    c = Clock()
    b = breaker(c, fail_threshold=1, base_open_s=5, half_open_trials=2)
    b.record_failure()
    assert b.state == "open"
    c.advance(4.9)
    assert b.allow() is False
    c.advance(0.2)
    assert b.allow() and b.allow()
    assert b.state == "half_open"
    assert b.allow() is False  # third concurrent trial refused


def test_two_trial_successes_close_it_and_reset_the_backoff():
    c = Clock()
    b = breaker(c, fail_threshold=1, base_open_s=5)
    b.record_failure()
    c.advance(6)
    b.allow(), b.record_success()
    b.allow(), b.record_success()
    assert b.state == "closed"
    b.record_failure()  # reopens with the *base* cool-down again, not a doubled one
    assert b.open_remaining() == pytest.approx(5.0)


def test_a_trial_failure_reopens_with_a_longer_cooldown_up_to_the_cap():
    c = Clock()
    b = breaker(c, fail_threshold=1, base_open_s=5, cap_open_s=12)
    b.record_failure()
    waits = []
    for _ in range(4):
        waits.append(b.open_remaining())
        c.advance(b.open_remaining() + 0.01)
        assert b.allow()
        b.record_failure()
    assert waits == pytest.approx([5, 10, 12, 12])  # 5·2^k, capped


def test_would_allow_does_not_consume_a_trial():
    c = Clock()
    b = breaker(c, fail_threshold=1, half_open_trials=1)
    b.record_failure()
    c.advance(10)
    assert b.would_allow() and b.would_allow()
    assert b.allow() is True
    assert b.allow() is False


def test_jitter_stays_within_bounds():
    c = Clock()
    seen = set()
    for seed in range(50):
        b = CircuitBreaker(
            clock=c, rng=random.Random(seed), fail_threshold=1, base_open_s=10, jitter=0.2
        )
        b.record_failure()
        seen.add(round(b.open_remaining(), 3))
        assert 8.0 <= b.open_remaining() <= 12.0
    assert len(seen) > 5  # it actually varies


def test_random_event_sequences_never_break_the_state_machine():
    """Seeded property test: whatever the interleaving, the invariants hold."""
    for seed in range(200):
        rng = random.Random(seed)
        c = Clock()
        b = breaker(c, fail_threshold=rng.randint(1, 6), half_open_trials=rng.randint(1, 3))
        admitted = 0
        for _ in range(120):
            op = rng.choice(["allow", "ok", "fail", "tick", "tick"])
            if op == "allow":
                if b.allow():
                    admitted += 1
                # Invariant: an open breaker never admits.
                elif b.state == "open":
                    assert b.open_remaining() >= 0
            elif op == "ok":
                b.record_success()
            elif op == "fail":
                b.record_failure()
            else:
                c.advance(rng.uniform(0, 20))
            assert b.state in ("closed", "open", "half_open")
            assert b.open_remaining() >= 0
            if b.state == "closed":
                assert b.would_allow()


# ── retry budget ──────────────────────────────────────────────────────────────


def test_retry_budget_allows_a_floor_when_traffic_is_low():
    rb = RetryBudget(clock=Clock(), ratio=0.2, min_retries=3)
    rb.note_request()
    assert [rb.try_retry() for _ in range(5)] == [True, True, True, False, False]


def test_retry_budget_scales_with_traffic_and_stops_a_storm():
    c = Clock()
    rb = RetryBudget(clock=c, ratio=0.2, window_s=10, min_retries=3)
    granted = 0
    for _ in range(500):  # every request fails and asks to retry
        rb.note_request()
        if rb.try_retry():
            granted += 1
    assert granted <= 0.2 * 500 + 3
    assert granted >= 0.2 * 500 * 0.9  # and it does not starve legitimate retries


def test_retry_budget_recovers_as_the_window_slides():
    c = Clock()
    rb = RetryBudget(clock=c, ratio=0.2, window_s=10, min_retries=2)
    for _ in range(50):
        rb.note_request()
    while rb.try_retry():
        pass
    assert rb.try_retry() is False
    c.advance(11)
    rb.note_request()
    assert rb.try_retry() is True


# ── bulkhead ──────────────────────────────────────────────────────────────────


async def test_bulkhead_limits_concurrency():
    bh = Bulkhead(max_concurrency=2, max_queue=10, queue_timeout=2)
    running = peak = 0

    async def work():
        nonlocal running, peak
        async with bh.slot():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(work() for _ in range(8)))
    assert peak == 2
    assert bh.inflight == 0 and bh.waiting == 0


async def test_bulkhead_sheds_when_the_queue_is_full():
    bh = Bulkhead(max_concurrency=1, max_queue=1, queue_timeout=2)
    release = asyncio.Event()

    async def hold():
        async with bh.slot():
            await release.wait()

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0)
    waiter = asyncio.create_task(hold())  # takes the one queue place
    await asyncio.sleep(0)
    with pytest.raises(ProviderError) as ei:
        async with bh.slot():
            pass
    assert ei.value.kind == "queue_full"
    assert ei.value.retryable is False  # capacity, not an upstream fault (ADR 0153 d4)
    release.set()
    await asyncio.gather(holder, waiter)


async def test_bulkhead_queue_wait_times_out_as_queue_full():
    bh = Bulkhead(max_concurrency=1, max_queue=5, queue_timeout=0.05)
    release = asyncio.Event()

    async def hold():
        async with bh.slot():
            await release.wait()

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0)
    with pytest.raises(ProviderError) as ei:
        async with bh.slot():
            pass
    assert ei.value.kind == "queue_full" and ei.value.retry_after is not None
    assert bh.waiting == 0  # the abandoned waiter is not leaked
    release.set()
    await holder


async def test_bulkhead_releases_the_slot_when_the_body_raises():
    bh = Bulkhead(max_concurrency=1, max_queue=0, queue_timeout=1)
    with pytest.raises(RuntimeError):
        async with bh.slot():
            raise RuntimeError("boom")
    async with bh.slot():  # not leaked
        pass


# ── deadline ──────────────────────────────────────────────────────────────────


def test_deadline_counts_down_and_expires():
    c = Clock()
    d = Deadline(3.0, clock=c)
    assert d.remaining() == pytest.approx(3.0) and not d.expired
    c.advance(2.5)
    assert d.remaining() == pytest.approx(0.5)
    c.advance(1)
    assert d.remaining() == 0.0 and d.expired


def test_deadline_min_with_a_caller_budget():
    c = Clock()
    assert Deadline.from_budget(10.0, caller_ms=2000, clock=c).remaining() == pytest.approx(2.0)
    assert Deadline.from_budget(10.0, caller_ms=None, clock=c).remaining() == pytest.approx(10.0)
    assert Deadline.from_budget(1.0, caller_ms=5000, clock=c).remaining() == pytest.approx(1.0)
