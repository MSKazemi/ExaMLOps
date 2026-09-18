"""The live stream carries the platform's events, not only this dashboard process's (ADR 0124).

Before the bridge, ``GET /api/v1/stream`` only ever saw what the process serving it did: an
approval from the CLI, a failed training run seen by the control plane, or a click on another
dashboard replica reached no browser. These tests pin the mapping onto the bus's channels, the
tenant rule, and the thread hand-off the bus depends on (its queues are not thread-safe).
"""

from __future__ import annotations

import asyncio
import threading

import backbone
import pytest
from realtime import bus

from examlops.events import envelope


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setitem(backbone._state, "stop", None)
    monkeypatch.setitem(backbone._state, "relayed", 0)
    monkeypatch.setitem(backbone._state, "error", None)
    monkeypatch.setenv("EXAMLOPS_EVENT_PUBLISHER", "nats")
    monkeypatch.delenv("DASHBOARD_BACKBONE", raising=False)


@pytest.mark.parametrize(
    ("topic", "channel"),
    [
        ("retrain.run_failed", "job.retrain_run_failed"),
        ("retrain.scheduled", "job.retrain_scheduled"),
        ("modelzoo.retrain_scheduled", "job.modelzoo_retrain_scheduled"),
        ("approval.approved", "approval.approved"),
        ("alert.drift", "alert.drift"),
        ("serving.traffic_changed", "deploy.serving_traffic_changed"),
        ("model.alias_changed", "deploy.model_alias_changed"),
        ("autopilot.cycle_complete", "event.autopilot_cycle_complete"),
    ],
)
def test_backbone_topics_land_on_the_dashboards_channels(topic, channel):
    assert backbone.channel_for(topic) == channel


def test_a_platform_wide_event_reaches_every_tenant_and_a_tenants_only_its_own():
    wide = envelope.build("approval.approved", {"model_id": "JPCP"}, event_id="outbox:1")
    scoped = envelope.build("approval.approved", {}, event_id="outbox:2", tenant="alpha")

    _, data, tenant = backbone.frame_for(wide)
    assert tenant is None and data["model_id"] == "JPCP"
    assert data["_event"]["id"] == "outbox:1"
    assert backbone.frame_for(scoped)[2] == "alpha"


def test_it_stays_off_unless_the_backbone_is_nats(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_EVENT_PUBLISHER", "log")
    assert (
        backbone.start(asyncio.new_event_loop(), watch=lambda *a: pytest.fail("watched")) is False
    )

    monkeypatch.setenv("EXAMLOPS_EVENT_PUBLISHER", "nats")
    monkeypatch.setenv("DASHBOARD_BACKBONE", "off")
    assert (
        backbone.start(asyncio.new_event_loop(), watch=lambda *a: pytest.fail("watched")) is False
    )


def test_a_broker_outage_never_stops_the_dashboard():
    def refuse(*_a):
        raise ConnectionError("nats unreachable")

    assert backbone.start(asyncio.new_event_loop(), watch=refuse) is False
    assert backbone.status()["error"] == "nats unreachable"


@pytest.mark.asyncio
async def test_an_event_from_the_nats_thread_reaches_a_browser_subscription():
    """The callback runs on the NATS thread; delivery must happen on the dashboard's loop."""
    feed: dict = {}

    def watch(subject, callback):
        feed["callback"] = callback
        return lambda: feed.setdefault("stopped", True)

    ops = bus.subscribe(("job.*",))
    other_tenant = bus.subscribe(("job.*",), tenant="beta")
    publish_threads: list[int] = []
    real_publish = bus.publish

    def recording_publish(*args, **kwargs):
        publish_threads.append(threading.get_ident())
        return real_publish(*args, **kwargs)

    bus.publish = recording_publish  # type: ignore[method-assign]
    try:
        assert backbone.start(asyncio.get_running_loop(), watch=watch) is True
        body = envelope.encode(
            envelope.build(
                "retrain.run_failed",
                {"model_name": "JPCP", "run_state": "FAILED"},
                event_id="outbox:9",
                tenant="alpha",
            )
        )
        sender = threading.Thread(target=feed["callback"], args=(body,))
        sender.start()
        sender.join()

        event = await asyncio.wait_for(ops.queue.get(), timeout=2)
        assert event.channel == "job.retrain_run_failed"
        assert event.data["model_name"] == "JPCP" and event.tenant == "alpha"
        assert other_tenant.queue.empty()  # tenant beta does not see tenant alpha's run
        assert backbone.status()["relayed"] == 1
        # The bus's asyncio queues are not thread-safe: it is touched on the loop's thread only.
        assert publish_threads == [threading.get_ident()]
    finally:
        bus.publish = real_publish  # type: ignore[method-assign]
        bus.unsubscribe(ops)
        bus.unsubscribe(other_tenant)
        backbone.stop()
    assert feed["stopped"] is True


def test_an_undecodable_message_is_dropped():
    loop = asyncio.new_event_loop()
    try:
        backbone._relay(loop, b"not a cloudevent")
    finally:
        loop.close()
    assert backbone.status()["relayed"] == 0
