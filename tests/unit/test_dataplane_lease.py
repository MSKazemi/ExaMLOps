"""``examlops.dataplane.lease.LeaseHeartbeat`` (task A4, dataplane Plan 2 batch S1).

The class used to be a private ``_LeaseHeartbeat`` inside ``examlops.dataplane.pull``; it now
lives here so a second lock holder (stream leader election is the first one lined up) can reuse
it. These tests exercise the public class directly, independent of any pull — the pull-lifecycle
behaviour (via ``pull.py``'s backward-compatible ``_LeaseHeartbeat`` subclass) is unchanged and
stays covered by ``tests/unit/test_dataplane_pull_lifecycle.py``.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from examlops.coordination import get_coordinator
from examlops.dataplane.lease import LeaseHeartbeat, fence_after


class _FakeCoordinator:
    """A minimal ``try_lock``/``unlock`` coordinator, scripted call-by-call — deterministic and
    not bound to the real coordinator's whole-second TTL granularity."""

    def __init__(self, results: list[bool]) -> None:
        self._results = results
        self.calls = 0

    def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
        self.calls += 1
        i = min(self.calls, len(self._results)) - 1
        return self._results[i]

    def unlock(self, key: str, holder: str) -> None:
        pass


def test_renewal_keeps_the_lease_alive():
    """The heartbeat re-takes the lock — same key, holder and TTL — roughly every ``ttl_s / 3``,
    for as long as it runs. Uses a scripted fake rather than timing a real lock's expiry against
    wall-clock renewals: under parallel test workers, a 1-second-floor real TTL (the DB
    coordinator's whole-second granularity) leaves too thin a margin against scheduling jitter to
    assert on reliably, and the mechanism under test is the repeated same-holder/TTL call, not the
    storage backend's own expiry arithmetic (that is `examlops.data.coordination`'s own
    responsibility, exercised elsewhere)."""

    class _Spy:
        def __init__(self) -> None:
            self.renewals: list[tuple[str, str, float]] = []

        def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
            self.renewals.append((key, holder, ttl_s))
            return True  # this holder is always the one renewing

        def unlock(self, key: str, holder: str) -> None:
            pass

    coord = _Spy()
    ttl = 0.15
    hb = LeaseHeartbeat(coord, "k", "me", ttl).start()
    try:
        time.sleep(ttl * 6)  # several renewal periods (ttl_s / 3 apart)
        assert len(coord.renewals) >= 3
        assert all(call == ("k", "me", ttl) for call in coord.renewals)
        assert not hb.lost
    finally:
        hb.stop()


def test_renewal_keeps_a_real_coordinator_lock_alive_past_its_raw_ttl():
    """End-to-end smoke test against the real ``DbCoordinator``: as long as the heartbeat is
    running, another holder still cannot take the key, well past what the raw TTL alone would
    allow. A generous TTL and sleep bound the flakiness the previous test avoids entirely."""
    coord = get_coordinator()
    key = "test:lease:renew-real"
    ttl = 3.0
    assert coord.try_lock(key, "me", ttl_s=ttl)
    hb = LeaseHeartbeat(coord, key, "me", ttl).start()
    try:
        time.sleep(ttl * 1.5)  # 1.5x the raw TTL: only a live renewal keeps another holder out
        assert not coord.try_lock(key, "other", ttl_s=ttl)
        assert not hb.lost
    finally:
        hb.stop()
        coord.unlock(key, "me")


def test_on_lost_fires_exactly_once_when_another_holder_takes_the_key():
    """``on_lost`` runs once, from the heartbeat thread, the moment a renewal is refused (the
    first ``try_lock`` — the initial take — succeeds; every renewal after it is refused, as if
    another holder took the key once this process stalled past its lease)."""
    coord = _FakeCoordinator([True, False, False, False])
    fired = threading.Event()
    seen: list[int] = []

    def on_lost() -> None:
        seen.append(1)
        fired.set()

    hb = LeaseHeartbeat(coord, "k", "me", 0.03, on_lost=on_lost).start()
    try:
        assert fired.wait(3), "on_lost was never called"
        time.sleep(0.2)  # give a would-be double-fire (or a still-running thread) time to show up
        assert hb.lost
        assert seen == [1]
        assert not hb._thread.is_alive()  # the loop returns right after on_lost, it does not spin
    finally:
        hb.stop()


def test_on_lost_exception_is_caught_and_the_lost_state_still_recorded():
    """A misbehaving ``on_lost`` cannot hide ``lost`` or kill the heartbeat thread silently."""

    def boom() -> None:
        raise RuntimeError("on_lost blew up")

    coord = _FakeCoordinator([False])  # refused from the very first renewal
    hb = LeaseHeartbeat(coord, "k", "me", 0.03, on_lost=boom).start()
    try:
        hb._thread.join(timeout=3)
        assert not hb._thread.is_alive()  # the exception did not leave the thread hung
        assert hb.lost
    finally:
        hb.stop()


def test_the_fence_clock_runs_from_when_a_renewal_was_sent_not_returned():
    """I1 regression: `_last_ok` must be stamped when a renewal is SENT, not when it returns —
    the coordinator's own expiry runs from about when the call reached it, so crediting a slow
    reply's RETURN time would push the fence deadline later than the key can actually survive.

    A first renewal is slow (``delay`` seconds) but succeeds; every renewal after it hangs, as a
    stall starting right after. Stamped on send, the fence fires at
    ``ttl/3 + fence_after(ttl)`` from start; stamped on return, `delay` seconds later than that —
    this asserts the tighter, correct bound."""
    ttl = 5.0  # -> fence_after(ttl) == 3.0
    delay = 1.0
    calls = [0]
    gate = threading.Event()

    class OneSlowRenewalThenHang:
        def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
            calls[0] += 1
            if calls[0] == 1:
                time.sleep(delay)  # a slow, but successful, first renewal
                return True
            gate.wait(30)  # the datastore then stalls completely
            return True

        def unlock(self, key: str, holder: str) -> None:
            pass

    hb = LeaseHeartbeat(OneSlowRenewalThenHang(), "k", "me", ttl, fence_on_error=True).start()
    try:
        deadline = time.monotonic() + ttl / 3.0 + fence_after(ttl) + 0.5
        while time.monotonic() < deadline and not hb.lost:
            time.sleep(0.02)
        assert hb.lost, "the fence should have fired by send_time + fence_after(ttl)"
    finally:
        gate.set()
        hb.stop()


def test_stop_does_not_release_the_lock_by_itself():
    """``stop()`` only stops renewing; releasing is the caller's job (as ``run_pull``'s own
    ``finally`` block does) — except when a renewal was already in flight when ``stop()`` ran,
    which this test does not trigger (the TTL is long enough that no tick fires in time)."""
    coord: Any = get_coordinator()
    key = "test:lease:stop"
    ttl = 5.0
    assert coord.try_lock(key, "me", ttl_s=ttl)
    hb = LeaseHeartbeat(coord, key, "me", ttl).start()
    hb.stop()
    try:
        assert not coord.try_lock(
            key, "other", ttl_s=ttl
        )  # still held: stop() alone releases nothing
    finally:
        coord.unlock(key, "me")
    assert coord.try_lock(key, "other", ttl_s=ttl)  # free once the caller releases it
    coord.unlock(key, "other")
