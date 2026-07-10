"""Realtime event bus + SSE gateway (F8 / ADR 0058, R3–R7)."""

import pytest
from realtime import EventBus, Subscription, sse_frame


def test_pattern_match_delivers_only_subscribed_channels():
    bus = EventBus()
    sub = bus.subscribe(("job.*",))
    assert bus.publish("job.started", {"id": 1}) == 1
    assert bus.publish("drift.critical", {"m": "JPCP"}) == 0  # not subscribed
    assert sub.queue.qsize() == 1
    assert sub.queue.get_nowait().channel == "job.started"


def test_tenant_filter_blocks_cross_tenant_events():
    bus = EventBus()
    bus.subscribe(("event.*",), tenant="acme")
    assert bus.publish("event.x", {}, tenant="other") == 0  # cross-tenant → blocked (R4)
    assert bus.publish("event.x", {}, tenant="acme") == 1
    assert bus.publish("event.x", {}, tenant=None) == 1  # broadcast reaches everyone


def test_operator_without_tenant_sees_all():
    bus = EventBus()
    bus.subscribe(("event.*",), tenant=None)  # operator
    assert bus.publish("event.x", {}, tenant="acme") == 1


def test_backpressure_drops_oldest_and_counts():
    sub = Subscription(patterns=("job.*",), buffer=2)
    from realtime import Event

    for i in range(5):
        sub.offer(Event("job.n", {"i": i}))
    assert sub.queue.qsize() == 2  # bounded
    assert sub.dropped == 3
    # Freshest events are kept (drop-oldest).
    kept = [sub.queue.get_nowait().data["i"], sub.queue.get_nowait().data["i"]]
    assert kept == [3, 4]


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    sub = bus.subscribe(("job.*",))
    bus.unsubscribe(sub)
    assert bus.publish("job.started", {}) == 0
    assert bus.subscriber_count == 0


def test_sse_frame_format():
    frame = sse_frame("job.started", {"id": 1})
    assert frame == 'event: job.started\ndata: {"id": 1}\n\n'


def test_parse_channels_expands_namespaces():
    from realtime import CHANNELS
    from routers.bff import _parse_channels

    assert _parse_channels("*") == tuple(f"{ns}.*" for ns in CHANNELS)
    assert _parse_channels("") == tuple(f"{ns}.*" for ns in CHANNELS)
    assert _parse_channels("job") == ("job.*",)  # bare namespace expanded
    assert _parse_channels("job.started,drift.*") == ("job.started", "drift.*")


@pytest.mark.asyncio
async def test_stream_requires_auth(client):
    r = await client.get("/api/v1/stream")
    assert r.status_code in (401, 403)
