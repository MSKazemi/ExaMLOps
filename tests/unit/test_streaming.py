"""SSE resume cursors + backoff reconnect (enterprise-readiness Phase 1, item 1.7).

Proves a reconnecting client replays only the gap after its Last-Event-ID, a client offline longer
than the ring gets a truncation signal (full refresh), and reconnect backoff grows exponentially,
is capped, and is jittered to avoid a thundering herd.
"""

from __future__ import annotations

from examlops.streaming import EventRing, backoff_delay, parse_last_event_id


def test_backoff_is_exponential_and_capped():
    # rand=0.5 → no net jitter (factor 1.0).
    d0 = backoff_delay(0, base=0.5, rand=0.5)
    d1 = backoff_delay(1, base=0.5, rand=0.5)
    d2 = backoff_delay(2, base=0.5, rand=0.5)
    assert d0 == 0.5 and d1 == 1.0 and d2 == 2.0
    assert backoff_delay(20, base=0.5, cap=30, rand=0.5) == 30.0  # capped


def test_backoff_jitter_bounds():
    lo = backoff_delay(3, base=1.0, jitter=0.5, rand=0.0)  # 1*8 * 0.5
    hi = backoff_delay(3, base=1.0, jitter=0.5, rand=1.0)  # 1*8 * 1.5
    assert lo == 4.0 and hi == 12.0
    assert all(backoff_delay(3, base=1.0, rand=r) >= 0 for r in (0, 0.5, 1))


def test_resume_replays_only_the_gap():
    ring = EventRing(maxlen=100)
    ring.publish("drift", {"m": 1})
    ring.publish("drift", {"m": 2})
    cursor = ring.latest_id  # client caught up here
    ring.publish("promo", {"m": 3})
    ring.publish("promo", {"m": 4})

    result = ring.since(cursor)
    assert [e["data"]["m"] for e in result["events"]] == [3, 4]  # only the missed events
    assert result["truncated"] is False
    assert result["cursor"] == ring.latest_id


def test_none_cursor_returns_all_buffered():
    ring = EventRing(maxlen=100)
    ring.publish("a", 1)
    ring.publish("a", 2)
    assert len(ring.since(None)["events"]) == 2


def test_truncation_when_offline_too_long():
    ring = EventRing(maxlen=3)  # tiny ring
    first = ring.publish("a", 1)  # id 1
    for i in range(5):  # push id 1 off the ring
        ring.publish("a", i + 2)
    result = ring.since(first)  # asking for id 1, which was evicted
    assert result["truncated"] is True  # client must do a full refresh


def test_parse_last_event_id():
    assert parse_last_event_id("42") == 42
    assert parse_last_event_id(None) is None
    assert parse_last_event_id("not-a-number") is None
