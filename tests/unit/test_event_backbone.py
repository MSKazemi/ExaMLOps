"""The event backbone publishes CloudEvents and consumes them at-least-once, safely (ADR 0124).

Until 2026-09-10 every event the platform emitted went to the `log` publisher and was forgotten:
`nats` was a stub that raised and nothing consumed anything. These tests pin the envelope, the
broker-side idempotency key, the relay's hand-off, and the consumer's inbox / retry / dead-letter
contract against in-memory fakes; the live-server proof is tests/integration/test_nats_backbone_live.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from examlops import events
from examlops.events import envelope
from examlops.events.consumer import EventConsumer
from examlops.events.nats_backend import subject_for

# ─── the envelope ──────────────────────────────────────────────────────────────


def test_an_outbox_row_becomes_a_cloudevent():
    row = {
        "id": 42,
        "topic": "retrain.scheduled",
        "payload": json.dumps({"model_name": "JPCP"}),
        "created_at": "2026-09-10 21:00:00",
        "actor": "alice",
        "tenant": "alpha",
    }

    event = envelope.from_outbox_row(row)

    assert event["specversion"] == "1.0"
    assert event["id"] == "outbox:42"
    assert event["type"] == "io.examlops.retrain.scheduled"
    assert event["time"] == "2026-09-10T21:00:00Z"
    assert event["examlopstenant"] == "alpha" and event["examlopsactor"] == "alice"
    assert event["data"] == {"model_name": "JPCP"}
    assert envelope.topic_of(event) == "retrain.scheduled"
    assert envelope.decode(envelope.encode(event)) == event


def test_extension_attribute_names_are_valid_cloudevents_names():
    event = envelope.build("t", {}, event_id="x")
    for name in event:
        assert name.isalnum() and name == name.lower(), name


def test_a_non_cloudevent_body_is_refused():
    with pytest.raises(ValueError):
        envelope.decode(b'{"hello": "world"}')


@pytest.mark.parametrize(
    ("topic", "subject"),
    [
        ("retrain.scheduled", "examlops.events.retrain.scheduled"),
        ("approval.approved", "examlops.events.approval.approved"),
        ("weird topic/*>", "examlops.events.weird_topic___"),
    ],
)
def test_topics_map_to_safe_subjects(topic, subject):
    assert subject_for(topic, "examlops.events") == subject


# ─── publishing ────────────────────────────────────────────────────────────────


class _FakeStream:
    prefix = "examlops.events"

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, str]] = []

    def publish(self, subject, body, *, msg_id):
        self.published.append((subject, body, msg_id))
        return False


def test_the_nats_publisher_sends_the_outbox_id_as_the_broker_dedup_key():
    stream = _FakeStream()
    publisher = events.NatsPublisher(stream=stream)

    publisher.publish("approval.rejected", {"model_id": "JPCP"}, event_id="outbox:7")

    subject, body, msg_id = stream.published[0]
    assert subject == "examlops.events.approval.rejected"
    assert msg_id == "outbox:7"
    assert envelope.decode(body)["data"] == {"model_id": "JPCP"}


def test_the_relay_hands_envelope_aware_publishers_the_whole_cloudevent(monkeypatch):
    stream = _FakeStream()
    monkeypatch.setattr(events, "get_publisher", lambda: events.NatsPublisher(stream=stream))
    events.publish("retrain.scheduled", {"model_name": "JPCP"})

    result = events.relay_once()

    assert result["published"] == 1
    body = envelope.decode(stream.published[0][1])
    assert body["type"] == "io.examlops.retrain.scheduled"
    assert body["examlopstenant"] == "default"
    assert body["time"].endswith("Z")


def test_legacy_publishers_keep_their_signature(monkeypatch):
    seen: list[tuple] = []

    class _Legacy:
        def publish(self, topic, payload, *, event_id):
            seen.append((topic, payload, event_id))

    monkeypatch.setattr(events, "get_publisher", lambda: _Legacy())
    events.publish("t.legacy", {"a": 1})
    events.relay_once()
    assert seen and seen[0][0] == "t.legacy" and seen[0][2].startswith("outbox:")


# ─── consuming ─────────────────────────────────────────────────────────────────


class _Msg:
    def __init__(self, body: bytes, delivered: int = 1) -> None:
        self.data = body
        self.metadata = SimpleNamespace(num_delivered=delivered)
        self.settled: list[str] = []

    async def ack(self):
        self.settled.append("ack")

    async def nak(self, delay=None):
        self.settled.append(f"nak:{delay}")


class _ConsumerStream(_FakeStream):
    def settle(self, coro):
        import asyncio

        return asyncio.run(coro)


def _event(n: int = 1) -> bytes:
    return envelope.encode(
        envelope.build("retrain.run_completed", {"n": n}, event_id=f"outbox:{n}")
    )


def test_a_handled_event_is_acked_and_recorded_so_redelivery_is_a_no_op():
    calls: list[str] = []
    consumer = EventConsumer("unit-a", lambda e: calls.append(e["id"]), stream=_ConsumerStream())

    first, again = _Msg(_event(1)), _Msg(_event(1), delivered=2)
    assert consumer.handle(first) == "handled"
    assert consumer.handle(again) == "duplicate"

    assert calls == ["outbox:1"]
    assert first.settled == ["ack"] and again.settled == ["ack"]


def test_a_failing_handler_is_retried_with_a_growing_delay():
    def boom(_event):
        raise RuntimeError("downstream down")

    consumer = EventConsumer("unit-b", boom, max_deliver=3, stream=_ConsumerStream())
    msg = _Msg(_event(2), delivered=1)

    assert consumer.handle(msg) == "retry"
    assert msg.settled == ["nak:2.0"]


def test_a_poison_event_is_parked_on_the_dead_letter_subject_and_acked():
    stream = _ConsumerStream()

    def boom(_event):
        raise RuntimeError("cannot process")

    consumer = EventConsumer("unit-c", boom, max_deliver=3, stream=stream)
    msg = _Msg(_event(3), delivered=3)

    assert consumer.handle(msg) == "dead"
    assert msg.settled == ["ack"]
    subject, body, msg_id = stream.published[-1]
    assert subject == "examlops.dlq.unit-c"
    parked = json.loads(body)
    assert parked["error"] == "cannot process" and parked["event"]["id"] == "outbox:3"
    assert msg_id == "dlq:unit-c:outbox:3"


def test_an_undecodable_message_is_parked_not_retried_forever():
    stream = _ConsumerStream()
    consumer = EventConsumer("unit-d", lambda e: None, stream=stream)
    msg = _Msg(b"not json")

    assert consumer.handle(msg) == "dead"
    assert stream.published[-1][0] == "examlops.dlq.unit-d"


def test_consumer_names_are_constrained():
    with pytest.raises(ValueError):
        EventConsumer("bad name!", lambda e: None, stream=_ConsumerStream())
