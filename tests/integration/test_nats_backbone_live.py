"""The event backbone against a live NATS JetStream server (ADR 0124).

Opt-in; it creates and deletes the stream it uses::

    docker run -d --name examlops-natstest -p 14222:4222 nats:2.14.6-alpine -js
    EXAMLOPS_NATS_TEST_URL=nats://localhost:14222 \\
        .venv/bin/pytest tests/integration/test_nats_backbone_live.py -v

The variable is deliberately not ``EXAMLOPS_NATS_URL`` so the suite cannot delete a real stream by
accident.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "platform" / "cli" / "src"))

URL = os.getenv("EXAMLOPS_NATS_TEST_URL", "")
pytestmark = [pytest.mark.live, pytest.mark.skipif(not URL, reason="set EXAMLOPS_NATS_TEST_URL")]


@pytest.fixture()
def js(monkeypatch, tmp_path):
    pytest.importorskip("nats")
    stream = f"EXA_TEST_{uuid.uuid4().hex[:8].upper()}"
    prefix = f"exatest{uuid.uuid4().hex[:6]}.events"
    monkeypatch.setenv("EXAMLOPS_NATS_URL", URL)
    monkeypatch.setenv("EXAMLOPS_NATS_STREAM", stream)
    monkeypatch.setenv("EXAMLOPS_NATS_SUBJECT_PREFIX", prefix)
    monkeypatch.setenv("EXAMLOPS_EVENT_PUBLISHER", "nats")
    from examlops import events
    from examlops.events import nats_backend

    nats_backend.reset_shared()
    events.reset_publisher()
    conn = nats_backend.shared()
    conn.ensure_stream()
    yield conn

    async def _drop():
        await conn._unsubscribe_all()
        js_ctx = await conn._connect()
        await js_ctx.delete_stream(stream)

    conn._loop.run(_drop(), 20)
    conn.close()
    nats_backend.reset_shared()
    events.reset_publisher()


def _stream_messages(conn) -> int:
    async def _info():
        js_ctx = await conn._connect()
        return (await js_ctx.stream_info(conn.stream)).state.messages

    return conn._loop.run(_info(), 10)


def test_the_relay_publishes_cloudevents_into_the_stream(js):
    from examlops import events

    events.publish("retrain.scheduled", {"model_name": "JPCP"})
    assert events.relay_once()["published"] == 1
    assert _stream_messages(js) == 1


def test_a_republished_outbox_event_is_one_stream_message(js):
    """The relay can publish and then crash before marking the row: the broker dedups on the id."""
    from examlops.events import envelope

    event = envelope.build("approval.approved", {"model_id": "JPCP"}, event_id="outbox:123")
    body = envelope.encode(event)
    subject = f"{js.prefix}.approval.approved"

    assert js.publish(subject, body, msg_id="outbox:123") is False
    assert js.publish(subject, body, msg_id="outbox:123") is True  # recognised as a duplicate
    assert _stream_messages(js) == 1


def test_a_durable_consumer_handles_each_event_once(js):
    from examlops import events
    from examlops.events.consumer import EventConsumer

    for n in range(3):
        events.publish("retrain.run_completed", {"n": n})
    events.relay_once()
    seen: list[int] = []
    consumer = EventConsumer(
        f"live-{uuid.uuid4().hex[:6]}", lambda e: seen.append(e["data"]["n"]), stream=js, wait=1.0
    )

    first = consumer.run_once()
    again = consumer.run_once()

    assert sorted(seen) == [0, 1, 2]
    assert first["handled"] == 3 and again["handled"] == 0


def test_a_poison_event_lands_on_the_dead_letter_subject(js):
    from examlops import events
    from examlops.events.consumer import EventConsumer

    events.publish("retrain.run_failed", {"n": 1})
    events.relay_once()
    name = f"poison-{uuid.uuid4().hex[:6]}"

    def boom(_event):
        raise RuntimeError("cannot process")

    consumer = EventConsumer(name, boom, stream=js, max_deliver=1, wait=1.0)
    outcome = consumer.run_once()

    assert outcome["dead"] == 1

    async def _read_dlq():
        js_ctx = await js._connect()
        sub = await js_ctx.pull_subscribe(f"examlops.dlq.{name}", stream=js.stream)
        msgs = await sub.fetch(1, timeout=3)
        await msgs[0].ack()
        return json.loads(msgs[0].data)

    parked = js._loop.run(_read_dlq(), 10)
    assert parked["error"] == "cannot process"
    assert parked["event"]["data"] == {"n": 1}


def test_the_consumer_loop_stops_when_asked(js):
    from examlops.events.consumer import EventConsumer

    stop = threading.Event()
    consumer = EventConsumer(f"loop-{uuid.uuid4().hex[:6]}", lambda e: None, stream=js, wait=0.5)
    t = threading.Thread(target=consumer.run_forever, args=(stop,))
    t.start()
    stop.set()
    t.join(10)
    assert not t.is_alive()


def test_backbone_stats_report_lag_and_dead_letters(js):
    """What the control plane's consumer-lag and dead-letter gauges read (plan P2.7)."""
    from examlops import events
    from examlops.events.consumer import EventConsumer

    for n in range(3):
        events.publish("retrain.run_failed", {"n": n})
    events.relay_once()
    idle = f"idle-{uuid.uuid4().hex[:6]}"
    parking = f"park-{uuid.uuid4().hex[:6]}"

    # A consumer that exists but has handled nothing is 3 behind.
    js.fetch(idle, f"{js.prefix}.>", batch=1, wait=0.5, max_deliver=5)

    def boom(_event):
        raise RuntimeError("cannot process")

    EventConsumer(parking, boom, stream=js, max_deliver=1, wait=1.0).run_once()

    stats = js.backbone_stats()

    assert stats["consumers"][parking]["pending"] == 0
    assert stats["consumers"][idle]["pending"] + stats["consumers"][idle]["ack_pending"] == 3
    assert stats["dlq"][parking] == 3


def test_the_serving_snapshot_reaches_a_replica_through_the_kv_bucket(js, monkeypatch):
    """ADR 0127: published → mirrored to NATS KV → read and verified by a serving replica."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from examlops import serving_snapshot
    from examlops.serving_snapshot import digest_of
    from serving.ray_serving.snapshot import SnapshotReader

    bucket = f"exatest-serving-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(serving_snapshot, "KV_BUCKET", bucket)
    content = {
        "models": {"jpcp": {"name": "jpcp", "aliases": {"Production": {"version": "7"}}}},
        "traffic": {},
        "shadow": {},
    }
    generation, published = serving_snapshot.publish(
        {"schema": 1, "digest": digest_of(content), **content}
    )
    reader = SnapshotReader(cache_path=Path(os.getenv("PLATFORM_DB", "/tmp/x")).parent / "s.json")
    monkeypatch.setattr(reader, "_from_db", lambda: None)  # prove the KV path on its own

    try:
        got = reader.newest()
        assert published and got["generation"] == generation
        assert reader.source == "kv"
        assert got["models"]["jpcp"]["aliases"]["Production"]["version"] == "7"
    finally:

        async def _drop_bucket():
            ctx = await js._connect()
            await ctx.delete_key_value(bucket)

        js._loop.run(_drop_bucket(), 10)


def test_a_broadcast_watch_sees_every_new_event_and_no_old_ones(js):
    """What each dashboard replica's live stream is fed by (plan P2.3)."""
    import time

    from examlops import events
    from examlops.events import envelope

    events.publish("retrain.scheduled", {"n": "before"})
    events.relay_once()
    seen_a: list[dict] = []
    seen_b: list[dict] = []
    stop_a = js.watch(None, lambda body: seen_a.append(envelope.decode(body)))
    stop_b = js.watch(None, lambda body: seen_b.append(envelope.decode(body)))
    try:
        events.publish("retrain.run_completed", {"n": "after"})
        events.relay_once()
        deadline = time.monotonic() + 5
        while (not seen_a or not seen_b) and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stop_a()
        stop_b()

    assert [e["data"]["n"] for e in seen_a] == ["after"]  # new events only
    assert [e["data"]["n"] for e in seen_b] == ["after"]  # and every watcher gets each one


def test_inference_telemetry_round_trips_bridge_to_consumer(js, tmp_path, monkeypatch):
    """The actual ADR 0123 decision-4 path, end to end: the bridge's publish call (not a fake)
    through a real NATS server to `handle_inference_telemetry_event` (not a fake), landing the
    same rows `write_drift_snapshot`/`write_input_snapshot` would have written directly.

    Publishes through `NatsPublisher` directly rather than importing the real bridge module,
    which needs the (not installed here) `dataplane-bus` client SDK and is unit-tested against a
    stub for exactly that reason (`test_dataplane_bus_bridge.py`). What that unit suite does not
    cover — and this does — is that the payload it hands `NatsPublisher` actually survives a
    real NATS round trip into `handle_inference_telemetry_event`.
    """
    from examlops.data.drift import handle_inference_telemetry_event
    from examlops.events import NatsPublisher
    from examlops.events.consumer import EventConsumer
    from examlops.platform_db import get_db, init_db

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "telemetry.db"))
    init_db()

    NatsPublisher(js).publish(
        "serving.inference_telemetry",
        {
            "model": "JPCP",
            "alias": "Production",
            "job_id": "job-live-1",
            "prediction": 42.0,
            "embedding_stats": {"norm": 5.0, "mean": 3.5, "std": 0.5},
        },
        event_id="telemetry:job-live-1",
    )

    consumer = EventConsumer(
        "telemetry-live-test", handle_inference_telemetry_event, stream=js, wait=2.0
    )
    result = consumer.run_once()

    assert result["handled"] == 1
    with get_db() as conn:
        drift = conn.execute("SELECT * FROM drift_snapshots WHERE model='JPCP'").fetchone()
        inp = conn.execute("SELECT * FROM input_snapshots WHERE model='JPCP'").fetchone()
    assert drift is not None and drift["prediction"] == 42.0
    assert inp is not None and round(inp["emb_norm"], 6) == 5.0
