"""Durable event consumers on the NATS JetStream backbone (ADR 0124).

A consumer is a name, a subject filter and a handler::

    from examlops.events.consumer import EventConsumer

    def on_run(event):  # a CloudEvents envelope (dict)
        ...

    EventConsumer("autopilot", on_run, subjects="examlops.events.retrain.>").run_forever(stop)

Delivery is at-least-once. The contract that makes it safe:

* **Inbox.** Before handling, the consumer checks ``event_inbox`` for ``(name, event id)`` and
  acknowledges a duplicate without calling the handler; after the handler succeeds it records the
  id. A crash *between* the handler and the record re-runs the handler once more, so handlers with
  external effects should be idempotent on ``event["id"]`` (or record inside their own transaction).
* **Retry, then park.** A handler that raises is negatively acknowledged with a growing delay. Once
  a message has been delivered ``max_deliver`` times it is published to
  ``examlops.dlq.<consumer>`` — with the envelope and the error — and acknowledged, so one poison
  event cannot stall the consumer. ``exa events tail --dlq <consumer>`` shows what was parked.
* **Durable progress.** The JetStream durable consumer remembers what was acknowledged; a consumer
  that was down resumes where it stopped, within the stream's retention.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from examlops.events import envelope as _envelope
from examlops.events import nats_backend

logger = logging.getLogger(__name__)

# Whatever a handler returns is ignored; raising is what asks for a retry.
Handler = Callable[[dict[str, Any]], object]
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")


class EventConsumer:
    def __init__(
        self,
        name: str,
        handler: Handler,
        *,
        subjects: str | None = None,
        max_deliver: int = 5,
        batch: int = 20,
        wait: float = 2.0,
        stream: nats_backend.JetStream | None = None,
    ) -> None:
        if not _NAME.match(name):
            raise ValueError("consumer name must be 1-63 chars of letters, digits, '_' or '-'")
        self.name = name
        self.handler = handler
        self._js = stream
        self.subjects = subjects
        self.max_deliver = max(1, max_deliver)
        self.batch = max(1, batch)
        self.wait = wait

    @property
    def js(self) -> nats_backend.JetStream:
        if self._js is None:
            self._js = nats_backend.shared()
        return self._js

    def _filter(self) -> str:
        return self.subjects or f"{self.js.prefix}.>"

    def _dead_letter(self, event: dict[str, Any] | None, raw: bytes, error: str) -> None:
        body = json.dumps(
            {
                "consumer": self.name,
                "error": error[:2000],
                "event": event,
                "raw": None if event is not None else raw.decode("utf-8", "replace")[:10000],
            },
            default=str,
        ).encode()
        event_id = str((event or {}).get("id") or "undecodable")
        self.js.publish(
            f"{nats_backend.DLQ_PREFIX}.{self.name}", body, msg_id=f"dlq:{self.name}:{event_id}"
        )

    def _span(self, event: dict[str, Any]) -> AbstractContextManager[Any]:
        """A CONSUMER span for the handler, parented on the producer's trace when it has one.

        The envelope's ``traceparent`` was captured in the request that wrote the event, so the
        handler's work — and anything it calls — lands in that request's trace. Without
        OpenTelemetry installed this is a no-op.
        """
        try:
            from opentelemetry import propagate, trace  # noqa: PLC0415 - optional
        except ImportError:
            return nullcontext()
        parent = event.get("traceparent")
        context = propagate.extract({"traceparent": parent}) if parent else None
        return trace.get_tracer("examlops.events").start_as_current_span(
            f"process {_envelope.topic_of(event)}",
            context=context,
            kind=trace.SpanKind.CONSUMER,
            attributes={
                "messaging.system": "nats",
                "messaging.operation.type": "process",
                "messaging.consumer.group.name": self.name,
                "messaging.message.id": str(event.get("id", "")),
                "cloudevents.event_type": str(event.get("type", "")),
            },
        )

    def handle(self, msg: Any) -> str:
        """Process one message; returns the outcome (handled|duplicate|retry|dead)."""
        from examlops.data.events import inbox_record, inbox_seen  # noqa: PLC0415

        delivered = int(getattr(getattr(msg, "metadata", None), "num_delivered", 1) or 1)
        try:
            event = _envelope.decode(msg.data)
        except Exception as exc:  # noqa: BLE001 - an undecodable message can only be parked
            self._dead_letter(None, msg.data, f"undecodable: {exc}")
            self.js.settle(msg.ack())
            return "dead"
        event_id = str(event["id"])
        if inbox_seen(self.name, event_id):
            self.js.settle(msg.ack())
            return "duplicate"
        try:
            with self._span(event):
                self.handler(event)
        except Exception as exc:  # noqa: BLE001 - the handler's failure is data, not a crash
            if delivered >= self.max_deliver:
                logger.error(
                    "Consumer %s parked %s after %d deliveries: %s",
                    self.name,
                    event_id,
                    delivered,
                    exc,
                )
                self._dead_letter(event, msg.data, str(exc))
                self.js.settle(msg.ack())
                return "dead"
            logger.warning(
                "Consumer %s failed %s (delivery %d): %s", self.name, event_id, delivered, exc
            )
            self.js.settle(msg.nak(delay=min(60.0, 2.0**delivered)))
            return "retry"
        inbox_record(self.name, event_id)
        self.js.settle(msg.ack())
        return "handled"

    def run_once(self) -> dict[str, int]:
        """Pull one batch and handle it. Returns counts per outcome."""
        counts = {"handled": 0, "duplicate": 0, "retry": 0, "dead": 0}
        for msg in self.js.fetch(
            self.name,
            self._filter(),
            batch=self.batch,
            wait=self.wait,
            max_deliver=self.max_deliver + 1,
        ):
            counts[self.handle(msg)] += 1
        return counts

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - a consumer outlives one bad cycle
                logger.warning("Consumer %s cycle failed: %s", self.name, exc)
                stop.wait(self.wait)
