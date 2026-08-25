"""Transactional outbox + event-publisher seam (enterprise-readiness Phase 1, item 1.3).

Proves the event backbone's publish side: events enqueue durably, the relay publishes each with a
stable deduplication ID, failures are retained (not dropped) for retry, concurrent relays don't
double-publish while their leases are healthy, and unavailable brokers fail loudly.
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

    captured: list[tuple[str, dict, str]] = []

    class _Capture:
        def publish(self, topic, payload, *, event_id):
            captured.append((topic, payload, event_id))

    events._publisher = _Capture()  # inject

    events.publish("drift.detected", {"model": "JPCP", "z": 3.1})
    events.publish("promotion.made", {"model": "JPCP", "version": 18})
    assert db.outbox_stats()["pending"] == 2

    result = events.relay_once()
    assert result == {"claimed": 2, "published": 2, "failed": 0}
    assert captured == [
        ("drift.detected", {"model": "JPCP", "z": 3.1}, "outbox:1"),
        ("promotion.made", {"model": "JPCP", "version": 18}, "outbox:2"),
    ]
    stats = db.outbox_stats()
    assert stats["pending"] == 0 and stats["published"] == 2


def test_relay_does_not_repeat_an_acknowledged_event(db):
    import examlops.events as events

    calls = {"n": 0}

    class _Count:
        def publish(self, topic, payload, *, event_id):
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
        def publish(self, topic, payload, *, event_id):
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


def test_poison_event_stops_after_retry_budget(db, monkeypatch):
    import examlops.events as events

    class _Broken:
        def publish(self, topic, payload, *, event_id):
            raise RuntimeError("permanent failure")

    monkeypatch.setenv("EXAMLOPS_EVENT_MAX_ATTEMPTS", "2")
    events._publisher = _Broken()
    events.publish("bad", {"value": object()})

    assert events.relay_once() == {"claimed": 1, "published": 0, "failed": 1}
    assert events.relay_once() == {"claimed": 1, "published": 0, "failed": 1}
    assert events.relay_once() == {"claimed": 0, "published": 0, "failed": 0}
    assert db.outbox_stats()["poison"] == 1


def test_relay_rejects_invalid_retry_budget(db, monkeypatch):
    import examlops.events as events

    monkeypatch.setenv("EXAMLOPS_EVENT_MAX_ATTEMPTS", "0")
    with pytest.raises(RuntimeError, match="must be greater than zero"):
        events.relay_once()


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
        def publish(self, topic, payload, *, event_id):
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


def test_unimplemented_broker_skeletons_fail_loudly(monkeypatch):
    import examlops.events as events

    for name in ("nats", "kafka"):
        monkeypatch.setenv("EXAMLOPS_EVENT_PUBLISHER", name)
        events.reset_publisher()
        with pytest.raises(RuntimeError, match="not configured"):
            events.get_publisher().publish("t", {}, event_id="outbox:1")
    monkeypatch.delenv("EXAMLOPS_EVENT_PUBLISHER", raising=False)
    events.reset_publisher()


def test_default_publisher_is_log(db):
    import examlops.events as events

    assert isinstance(events.get_publisher(), events.LogPublisher)


class _FakeRedis:
    def __init__(self):
        self.calls = []

    def xadd(self, stream, fields, *, maxlen, approximate):
        self.calls.append((stream, fields, maxlen, approximate))
        return "1-0"


def test_redis_stream_publisher_sends_stable_envelope(monkeypatch):
    import examlops.events as events

    monkeypatch.setenv("EXAMLOPS_REDIS_EVENT_STREAM", "exa.events.test")
    client = _FakeRedis()
    publisher = events.RedisStreamsPublisher(client)
    publisher.publish("drift.detected", {"z": 3.1}, event_id="outbox:42")
    assert client.calls == [
        (
            "exa.events.test",
            {"event_id": "outbox:42", "topic": "drift.detected", "payload": '{"z":3.1}'},
            100000,
            True,
        )
    ]


def test_redis_stream_publisher_requires_url(monkeypatch):
    import examlops.events as events

    monkeypatch.delenv("EXAMLOPS_REDIS_URL", raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        events.RedisStreamsPublisher()


def test_unknown_publisher_fails_closed(monkeypatch):
    import examlops.events as events

    monkeypatch.setenv("EXAMLOPS_EVENT_PUBLISHER", "typo")
    events.reset_publisher()
    with pytest.raises(RuntimeError, match="unsupported EXAMLOPS_EVENT_PUBLISHER"):
        events.get_publisher()
    events.reset_publisher()
