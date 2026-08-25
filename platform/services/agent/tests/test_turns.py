"""Distributed per-session graph-turn lease behavior."""

from __future__ import annotations

import threading

import pytest
from skipper.turns import TurnBusy, TurnCoordinationUnavailable, acquire_turn


class _Coordinator:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self.holders: dict[str, str] = {}
        self.calls: list[tuple[str, str, float]] = []

    def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
        with self._guard:
            self.calls.append((key, holder, ttl_s))
            current = self.holders.get(key)
            if current not in (None, holder):
                return False
            self.holders[key] = holder
            return True

    def unlock(self, key: str, holder: str) -> None:
        with self._guard:
            if self.holders.get(key) == holder:
                del self.holders[key]


def test_same_session_has_exactly_one_concurrent_lease():
    coordinator = _Coordinator()
    barrier = threading.Barrier(8)
    leases = []
    busy = []
    guard = threading.Lock()

    def contend() -> None:
        barrier.wait()
        try:
            lease = acquire_turn("owned-session", coordinator=coordinator, ttl_s=30)
        except TurnBusy:
            with guard:
                busy.append(True)
        else:
            with guard:
                leases.append(lease)

    workers = [threading.Thread(target=contend) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert len(leases) == 1
    assert len(busy) == 7
    leases[0].release()


def test_release_allows_next_turn_and_different_sessions_do_not_conflict():
    coordinator = _Coordinator()
    first = acquire_turn("one", coordinator=coordinator, ttl_s=17)
    other = acquire_turn("two", coordinator=coordinator, ttl_s=17)
    with pytest.raises(TurnBusy):
        acquire_turn("one", coordinator=coordinator, ttl_s=17)

    first.release()
    replacement = acquire_turn("one", coordinator=coordinator, ttl_s=17)
    assert all(call[2] == 17 for call in coordinator.calls)
    assert all(
        "owned-session" not in call[0] and "one" not in call[0] for call in coordinator.calls
    )
    replacement.release()
    other.release()


def test_coordinator_failure_is_fail_closed():
    class BrokenCoordinator:
        def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
            raise OSError("down")

    with pytest.raises(TurnCoordinationUnavailable, match="coordination is unavailable"):
        acquire_turn("session", coordinator=BrokenCoordinator())


def test_live_turn_renews_its_bounded_lease():
    class ObservedCoordinator(_Coordinator):
        def __init__(self) -> None:
            super().__init__()
            self.renewed = threading.Event()

        def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
            acquired = super().try_lock(key, holder, ttl_s)
            if len(self.calls) > 1:
                self.renewed.set()
            return acquired

    coordinator = ObservedCoordinator()
    lease = acquire_turn("long-turn", coordinator=coordinator, ttl_s=1)
    assert coordinator.renewed.wait(timeout=0.8)
    assert {call[2] for call in coordinator.calls} == {1.0}
    lease.release()
