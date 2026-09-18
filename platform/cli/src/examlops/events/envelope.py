"""CloudEvents 1.0 envelopes for everything the event backbone carries (ADR 0124).

One envelope on every publisher, so a consumer never depends on which broker carried an event.
Structured JSON mode: the envelope *is* the message body, ``data`` holds the domain payload.

Extension attributes (lower-case alphanumerics, per the spec): ``examlopstenant``,
``examlopsactor`` and ``traceparent`` (the CloudEvents distributed-tracing extension): the trace
context of the transaction that wrote the event, captured into the outbox row at enqueue time, so
a consumer continues the producer's trace — not the relay's.
A registered topic also carries ``dataschema`` naming the JSON Schema of its ``data``
(:mod:`examlops.events.schemas`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

SPEC_VERSION = "1.0"
TYPE_PREFIX = "io.examlops."
DEFAULT_SOURCE = "/examlops/platform"
_CURRENT_SPAN = object()  # build(): "take the active span", as opposed to an explicit None


def event_type(topic: str) -> str:
    """``retrain.scheduled`` → ``io.examlops.retrain.scheduled``."""
    return f"{TYPE_PREFIX}{topic}"


def topic_of(envelope: dict[str, Any]) -> str:
    kind = str(envelope.get("type", ""))
    return kind[len(TYPE_PREFIX) :] if kind.startswith(TYPE_PREFIX) else kind


def _rfc3339(value: Any) -> str:
    """Outbox timestamps are SQLite-shaped (``YYYY-MM-DD HH:MM:SS``, UTC); emit RFC 3339."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    elif value:
        text = str(value).replace(" ", "T")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            moment = datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    else:
        moment = datetime.now(UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def current_traceparent() -> str | None:
    """The W3C ``traceparent`` of the active OpenTelemetry span, or ``None`` when there is none."""
    try:
        from opentelemetry import trace  # noqa: PLC0415 - optional

        ctx = trace.get_current_span().get_span_context()
        if not ctx.is_valid:
            return None
        return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{int(ctx.trace_flags):02x}"
    except Exception:  # noqa: BLE001 - tracing is optional; an envelope without it is valid
        return None


def build(
    topic: str,
    data: dict[str, Any],
    *,
    event_id: str,
    time: Any = None,
    actor: str | None = None,
    tenant: str | None = None,
    source: str = DEFAULT_SOURCE,
    traceparent: str | None | object = _CURRENT_SPAN,
) -> dict[str, Any]:
    """A structured-mode CloudEvent for one outbox event.

    ``traceparent`` is the producer's trace context. An outbox row carries the one captured when
    it was written (``None`` if no trace was active then); a caller that builds an event directly
    gets the span active now.
    """
    envelope: dict[str, Any] = {
        "specversion": SPEC_VERSION,
        "id": event_id,
        "source": source,
        "type": event_type(topic),
        "time": _rfc3339(time),
        "datacontenttype": "application/json",
        "examlopstenant": tenant or "default",
        "examlopsactor": actor or "system",
        "data": data,
    }
    from examlops.events.schemas import SCHEMAS, dataschema_uri  # noqa: PLC0415

    if topic in SCHEMAS:  # a registered contract; hand-published topics carry none (plan P2.6)
        envelope["dataschema"] = dataschema_uri(topic)
    if traceparent is _CURRENT_SPAN:
        traceparent = current_traceparent()
    if traceparent:
        envelope["traceparent"] = traceparent
    return envelope


def from_outbox_row(row: dict[str, Any]) -> dict[str, Any]:
    """The envelope for a claimed outbox row (``claim_outbox_batch`` output)."""
    return build(
        row["topic"],
        json.loads(row["payload"]),
        event_id=f"outbox:{row['id']}",
        time=row.get("created_at"),
        actor=row.get("actor"),
        tenant=row.get("tenant"),
        traceparent=row.get("traceparent"),
    )


def encode(envelope: dict[str, Any]) -> bytes:
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), default=str).encode()


def decode(body: bytes) -> dict[str, Any]:
    envelope = json.loads(body)
    if envelope.get("specversion") != SPEC_VERSION or "id" not in envelope:
        raise ValueError("not a CloudEvents 1.0 structured-mode message")
    return envelope
