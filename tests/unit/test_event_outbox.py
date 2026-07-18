"""Transactional outbox + event-publisher seam (enterprise-readiness Phase 1, item 1.3).

Proves the event backbone's publish side: events enqueue durably, the relay publishes each
exactly once through the configured publisher, failures are retained (not dropped) for retry,
concurrent relays don't double-publish, and the broker skeletons fail loudly.
"""

from __future__ import annotations

import threading

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_EVENT_PUBLISHER", raising=False)
    import examlops.events as events
    import examlops.platform_db as pdb

    pdb.init_db()
    events.reset_publisher()
    return pdb


def test_publish_enqueues_then_relay_publishes(db):
    import examlops.events as events

    captured: list[tuple[str, dict]] = []

    class _Capture:
        def publish(self, topic, payload):
            captured.append((topic, payload))

    events._publisher = _Capture()  # inject

    events.publish("drift.detected", {"model": "JPCP", "z": 3.1})
    events.publish("promotion.made", {"model": "JPCP", "version": 18})
    assert db.outbox_stats()["pending"] == 2

    result = events.relay_once()
    assert result == {"claimed": 2, "published": 2, "failed": 0}
    assert captured == [
        ("drift.detected", {"model": "JPCP", "z": 3.1}),
        ("promotion.made", {"model": "JPCP", "version": 18}),
    ]
    stats = db.outbox_stats()
    assert stats["pending"] == 0 and stats["published"] == 2


def test_relay_is_exactly_once_across_two_passes(db):
    import examlops.events as events

    calls = {"n": 0}

    class _Count:
        def publish(self, topic, payload):
            calls["n"] += 1

    events._publisher = _Count()
    events.publish("t", {"a": 1})
    events.relay_once()
    events.relay_once()  # nothing left to publish
    assert calls["n"] == 1


def test_failed_publish_is_retained_for_retry(db):
    import examlops.events as events

    state = {"fail": True}

    class _Flaky:
        def publish(self, topic, payload):
            if state["fail"]:
                raise RuntimeError("broker down")

    events._publisher = _Flaky()
    events.publish("t", {"a": 1})

    r1 = events.relay_once()
    assert r1["failed"] == 1 and r1["published"] == 0
    assert db.outbox_stats()["pending"] == 1  # retained, not dropped

    state["fail"] = False
    r2 = events.relay_once()
    assert r2["published"] == 1
    assert db.outbox_stats()["pending"] == 0


def test_enqueue_in_existing_transaction_is_atomic(db):
    """An event enqueued on an open conn commits with the domain write in one txn."""
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO drift_snapshots (model, alias, prediction) VALUES ('M','Production',1.0)"
        )
        db.enqueue_event("drift.snapshot", {"model": "M"}, conn=conn)
    assert db.outbox_stats()["pending"] == 1


def test_concurrent_relays_do_not_double_publish(db):
    import examlops.events as events

    lock = threading.Lock()
    published: list[int] = []

    class _Rec:
        def publish(self, topic, payload):
            with lock:
                published.append(payload["i"])

    events._publisher = _Rec()
    for i in range(20):
        events.publish("t", {"i": i})

    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        events.relay_once(limit=50)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(published) == list(range(20))  # each event published exactly once


def test_broker_skeletons_fail_loudly(monkeypatch):
    import examlops.events as events

    for name in ("nats", "kafka", "redis"):
        monkeypatch.setenv("EXAMLOPS_EVENT_PUBLISHER", name)
        events.reset_publisher()
        with pytest.raises(RuntimeError, match="not configured"):
            events.get_publisher().publish("t", {})
    monkeypatch.delenv("EXAMLOPS_EVENT_PUBLISHER", raising=False)
    events.reset_publisher()


def test_default_publisher_is_log(db):
    import examlops.events as events

    assert isinstance(events.get_publisher(), events.LogPublisher)
