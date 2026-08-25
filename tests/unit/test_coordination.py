"""Externalized coordination primitives (enterprise-readiness Phase 1, item 1.2).

Proves the coordination seam replaces in-process state: a named lock has exactly one holder
(even under concurrency), idempotency keys dedup retried triggers, the rate limiter enforces a
fixed window, and the Redis backend fails loudly rather than silently skipping coordination.
"""

from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture
def coord(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_COORDINATOR", raising=False)
    import examlops.coordination as c
    import examlops.platform_db as pdb

    pdb.init_db()
    c.reset_coordinator()
    return c.get_coordinator()


def test_lock_single_holder_and_release(coord):
    assert coord.try_lock("poller", "replica-a", 60) is True
    assert coord.try_lock("poller", "replica-b", 60) is False  # a holds it
    assert coord.try_lock("poller", "replica-a", 60) is True  # re-entrant for the owner
    coord.unlock("poller", "replica-a")
    assert coord.try_lock("poller", "replica-b", 60) is True  # freed


def test_lock_expires(coord):
    assert coord.try_lock("k", "a", 1) is True
    time.sleep(1.2)
    assert coord.try_lock("k", "b", 60) is True  # a's lock expired


def test_unlock_only_by_owner(coord):
    assert coord.try_lock("k", "a", 60) is True
    coord.unlock("k", "b")  # b doesn't own it → no-op
    assert coord.try_lock("k", "b", 60) is False  # a still holds it


def test_idempotency_first_seen_then_duplicate(coord):
    assert coord.first_seen("retrain:JPCP:req-1", 3600) is True  # first → do the work
    assert coord.first_seen("retrain:JPCP:req-1", 3600) is False  # duplicate → skip
    assert coord.first_seen("retrain:JPCP:req-2", 3600) is True  # different key → do it


def test_rate_limit_fixed_window(coord):
    allowed = [coord.allow("retrain", limit=3, window_s=60) for _ in range(5)]
    assert allowed == [True, True, True, False, False]  # 3 allowed, then blocked in the window


def test_rate_limit_window_resets(coord):
    assert coord.allow("b", limit=1, window_s=1) is True
    assert coord.allow("b", limit=1, window_s=1) is False
    time.sleep(1.1)
    assert coord.allow("b", limit=1, window_s=1) is True  # new window


def test_lock_exactly_one_winner_under_concurrency(coord):
    wins: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        got = coord.try_lock("leader", f"replica-{i}", 60)
        with lock:
            wins.append(got)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for w in wins if w) == 1  # leader election: exactly one


def test_redis_backend_fails_loudly_without_url(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_COORDINATOR", "redis")
    monkeypatch.delenv("EXAMLOPS_REDIS_URL", raising=False)
    import examlops.coordination as c

    c.reset_coordinator()
    with pytest.raises(RuntimeError, match="not configured"):
        c.get_coordinator()
    monkeypatch.delenv("EXAMLOPS_COORDINATOR", raising=False)
    c.reset_coordinator()


class _FakeRedis:
    """Small semantic fake for exercising the coordinator's Redis command contract."""

    def __init__(self):
        self.values: dict[str, str | int] = {}
        self.expiry_ms: dict[str, int] = {}

    def eval(self, script, key_count, key, *args):
        assert key_count == 1
        if "redis.call('incr'" in script:
            value = int(self.values.get(key, 0)) + 1
            self.values[key] = value
            if value == 1:
                self.expiry_ms[key] = int(args[0])
            return value
        holder = str(args[0])
        if "redis.call('pexpire'" in script:
            if self.values.get(key) == holder:
                self.expiry_ms[key] = int(args[1])
                return 1
            if key not in self.values:
                self.values[key] = holder
                self.expiry_ms[key] = int(args[1])
                return 1
            return 0
        if self.values.get(key) == holder:
            del self.values[key]
            self.expiry_ms.pop(key, None)
            return 1
        return 0

    def set(self, key, value, *, nx, px):
        assert nx is True
        if key in self.values:
            return False
        self.values[key] = value
        self.expiry_ms[key] = px
        return True


def test_redis_lock_is_owned_reentrant_and_safe_to_release(monkeypatch):
    import examlops.coordination as c

    fake = _FakeRedis()
    coord = c.RedisCoordinator(fake)
    assert coord.try_lock("leader", "replica-a", 10) is True
    assert coord.try_lock("leader", "replica-b", 10) is False
    assert coord.try_lock("leader", "replica-a", 20) is True
    assert fake.expiry_ms["examlops:coord:lock:leader"] == 20_000
    coord.unlock("leader", "replica-b")
    assert coord.try_lock("leader", "replica-b", 10) is False
    coord.unlock("leader", "replica-a")
    assert coord.try_lock("leader", "replica-b", 10) is True


def test_redis_idempotency_and_rate_limit_are_atomic():
    import examlops.coordination as c

    coord = c.RedisCoordinator(_FakeRedis())
    assert coord.first_seen("request-1", 60) is True
    assert coord.first_seen("request-1", 60) is False
    assert [coord.allow("api", 2, 60) for _ in range(3)] == [True, True, False]


def test_redis_rejects_invalid_ttls_and_limits():
    import examlops.coordination as c

    coord = c.RedisCoordinator(_FakeRedis())
    with pytest.raises(ValueError, match="greater than zero"):
        coord.try_lock("k", "holder", 0)
    with pytest.raises(ValueError, match="greater than zero"):
        coord.first_seen("k", -1)
    assert coord.allow("bucket", 0, 60) is False


def test_unknown_backend_fails_closed(monkeypatch):
    import examlops.coordination as c

    monkeypatch.setenv("EXAMLOPS_COORDINATOR", "typo")
    c.reset_coordinator()
    with pytest.raises(RuntimeError, match="unsupported EXAMLOPS_COORDINATOR"):
        c.get_coordinator()
    c.reset_coordinator()
