"""An event continues the trace of the request that produced it — not the relay's (plan P2.5).

The envelope's ``traceparent`` used to be read when the *relay* published the event. The relay
runs on a timer in its own thread, so at best it attached the relay's span and at worst nothing,
and a consumer could never be joined to the API call that caused the event. The trace context is
now written into the outbox row with the event, inside the producing transaction.
"""

from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from examlops import events  # noqa: E402
from examlops.events import envelope  # noqa: E402
from examlops.events.consumer import EventConsumer  # noqa: E402


@pytest.fixture()
def tracer():
    """A private provider: nothing global is installed, so parallel tests cannot interfere."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    tracer.exporter = exporter  # type: ignore[attr-defined]
    tracer.provider = provider  # type: ignore[attr-defined]
    return tracer


class _Capture:
    def __init__(self) -> None:
        self.envelopes: list[dict] = []

    def publish(self, topic, payload, *, event_id):  # legacy signature, unused
        raise AssertionError("the relay should hand over the whole envelope")

    def publish_event(self, event):
        self.envelopes.append(event)


def _relay(monkeypatch) -> list[dict]:
    capture = _Capture()
    monkeypatch.setattr(events, "get_publisher", lambda: capture)
    events.relay_once()
    return capture.envelopes


def _traceparent(span) -> str:
    ctx = span.get_span_context()
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{int(ctx.trace_flags):02x}"


def test_the_event_carries_the_producers_trace_not_the_relays(tracer, monkeypatch):
    with tracer.start_as_current_span("POST /v1/retrain") as request:
        events.publish(
            "retrain.scheduled", {"model_name": "J", "dataset_name": "D", "flow_run_id": "f"}
        )
        producer = _traceparent(request)

    with tracer.start_as_current_span("relay tick"):  # the relay's own span must not leak in
        [event] = _relay(monkeypatch)

    assert event["traceparent"] == producer


def test_an_event_written_outside_any_trace_has_none(tracer, monkeypatch):
    events.publish(
        "retrain.scheduled", {"model_name": "J", "dataset_name": "D", "flow_run_id": "f"}
    )

    with tracer.start_as_current_span("relay tick"):
        [event] = _relay(monkeypatch)

    assert "traceparent" not in event


def test_building_an_event_directly_still_takes_the_active_span(tracer):
    with tracer.start_as_current_span("direct") as span:
        event = envelope.build("ops.note", {}, event_id="x")
    assert event["traceparent"] == _traceparent(span)


class _Msg:
    def __init__(self, body: bytes) -> None:
        self.data = body
        self.metadata = type("M", (), {"num_delivered": 1})()

    async def ack(self):
        pass


class _Stream:
    prefix = "examlops.events"

    def settle(self, coro):
        import asyncio

        return asyncio.run(coro)


def test_the_consumer_handles_the_event_inside_the_producers_trace(tracer, monkeypatch):
    monkeypatch.setattr(trace, "get_tracer", lambda *a, **k: tracer)
    with tracer.start_as_current_span("POST /approvals/x/approve") as request:
        producer_ctx = request.get_span_context()
        body = envelope.encode(envelope.build("approval.approved", {}, event_id="outbox:9"))

    seen: list = []
    consumer = EventConsumer(
        "trace-test", lambda e: seen.append(trace.get_current_span()), stream=_Stream()
    )
    assert consumer.handle(_Msg(body)) == "handled"

    handler_ctx = seen[0].get_span_context()
    assert handler_ctx.trace_id == producer_ctx.trace_id
    [finished] = [s for s in tracer.exporter.get_finished_spans() if s.name.startswith("process")]
    assert finished.parent.span_id == producer_ctx.span_id
    assert finished.kind == trace.SpanKind.CONSUMER
    assert finished.attributes["messaging.consumer.group.name"] == "trace-test"
