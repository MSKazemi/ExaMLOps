"""Live Redis contract tests for cross-replica coordination.

Set ``EXAMLOPS_REDIS_TEST_URL`` to opt in. CI and local preflight skip this module when no
disposable Redis endpoint is available.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture
def coordinator(monkeypatch):
    url = os.getenv("EXAMLOPS_REDIS_TEST_URL")
    if not url:
        pytest.skip("EXAMLOPS_REDIS_TEST_URL is not set")

    import examlops.coordination as coordination

    prefix = f"examlops:test:{uuid.uuid4().hex}"
    monkeypatch.setenv("EXAMLOPS_COORDINATOR", "redis")
    monkeypatch.setenv("EXAMLOPS_REDIS_URL", url)
    monkeypatch.setenv("EXAMLOPS_REDIS_PREFIX", prefix)
    monkeypatch.setenv("EXAMLOPS_REDIS_EVENT_STREAM", f"{prefix}:events")
    coordination.reset_coordinator()
    instance = coordination.get_coordinator()
    yield instance
    client = instance._client
    keys = list(client.scan_iter(match=f"{prefix}:*"))
    if keys:
        client.delete(*keys)
    coordination.reset_coordinator()


def test_live_redis_coordination_contract(coordinator):
    assert coordinator.try_lock("leader", "replica-a", 30) is True
    assert coordinator.try_lock("leader", "replica-b", 30) is False
    assert coordinator.try_lock("leader", "replica-a", 60) is True

    coordinator.unlock("leader", "replica-b")
    assert coordinator.try_lock("leader", "replica-b", 30) is False
    coordinator.unlock("leader", "replica-a")
    assert coordinator.try_lock("leader", "replica-b", 30) is True

    assert coordinator.first_seen("request-1", 30) is True
    assert coordinator.first_seen("request-1", 30) is False
    assert [coordinator.allow("retrain", 2, 30) for _ in range(3)] == [True, True, False]


def test_concurrent_lock_contention_has_exactly_one_winner(coordinator):
    contenders = 24
    barrier = threading.Barrier(contenders)

    def contend(index: int) -> bool:
        barrier.wait(timeout=5)
        return coordinator.try_lock("election", f"replica-{index}", 30)

    with ThreadPoolExecutor(max_workers=contenders) as pool:
        futures = [pool.submit(contend, index) for index in range(contenders)]
        results = [future.result(timeout=5) for future in futures]

    assert results.count(True) == 1
    assert results.count(False) == contenders - 1


def test_expired_lock_fails_over_without_old_holder_deleting_new_lease(coordinator):
    assert coordinator.try_lock("poller", "replica-a", 0.15) is True

    deadline = time.monotonic() + 2
    while not coordinator.try_lock("poller", "replica-b", 1):
        assert time.monotonic() < deadline, "Redis lease did not expire within the bounded wait"
        time.sleep(0.02)

    coordinator.unlock("poller", "replica-a")
    assert coordinator.try_lock("poller", "replica-c", 1) is False
    coordinator.unlock("poller", "replica-b")
    assert coordinator.try_lock("poller", "replica-c", 1) is True


def test_concurrent_idempotency_dedup_has_exactly_one_first_seen(coordinator):
    contenders = 24
    barrier = threading.Barrier(contenders)

    def mark_seen() -> bool:
        barrier.wait(timeout=5)
        return coordinator.first_seen("shared-request", 30)

    with ThreadPoolExecutor(max_workers=contenders) as pool:
        futures = [pool.submit(mark_seen) for _ in range(contenders)]
        results = [future.result(timeout=5) for future in futures]

    assert results.count(True) == 1
    assert results.count(False) == contenders - 1


def test_live_redis_stream_event_envelope(coordinator):
    import examlops.events as events

    publisher = events.RedisStreamsPublisher(coordinator._client)
    publisher.publish("retrain.started", {"model": "JPCP"}, event_id="outbox:42")

    rows = coordinator._client.xrange(os.environ["EXAMLOPS_REDIS_EVENT_STREAM"])
    assert len(rows) == 1
    _, envelope = rows[0]
    assert envelope == {
        "event_id": "outbox:42",
        "topic": "retrain.started",
        "payload": '{"model":"JPCP"}',
    }
