"""NovaFabric event backbone — transactional-outbox publisher seam (Phase 1 item 1.3).

The enterprise blueprint replaces O(models×replicas) polling and the in-process realtime
singleton with an event backbone: drift/promotion/retrain/audit/inference events are published
**once** and every surface subscribes. This module is the publish side.

Design (mirrors the ADR-0074 provider seam + the item-0.1 StorageBackend seam):

  * domain code calls :func:`publish` (or ``platform_db.enqueue_event`` inside an existing txn) —
    the event lands in the ``event_outbox`` table, committed atomically with the domain write;
  * a **relay** (:func:`relay_once`) claims unpublished rows and hands them to an
    :class:`EventPublisher`, marking each published/failed. Delivery is at-least-once: every
    publication carries a stable outbox event ID so consumers can deduplicate crash replays;
  * the publisher is swappable via ``EXAMLOPS_EVENT_PUBLISHER``. The default ``log`` publisher is
    dependency-free (works offline, in tests, single-node dev), and ``redis`` publishes a stable
    envelope to Redis Streams. ``nats``/``kafka`` are placeholders that fail loudly instead of
    silently dropping events.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class EventPublisher(Protocol):
    """Publish a single event to the backbone. Must raise on failure (the relay retries)."""

    def publish(self, topic: str, payload: dict[str, Any], *, event_id: str) -> None: ...


class LogPublisher:
    """Default, dependency-free publisher: emits the event to the platform log.

    Single-node/dev/test appropriate — the outbox still gives durable, at-least-once relay
    semantics; only the fan-out is local. Swap in a broker publisher for multi-node fan-out.
    """

    def publish(self, topic: str, payload: dict[str, Any], *, event_id: str) -> None:
        logger.info("event published id=%s topic=%s payload=%s", event_id, topic, payload)


class _BrokerSkeleton:
    """Base for real-broker publishers that aren't wired yet — fails loudly, never silently drops."""

    _NAME = "broker"
    _ENV = "EXAMLOPS_EVENT_BROKER_URL"

    def publish(self, topic: str, payload: dict[str, Any], *, event_id: str) -> None:
        raise RuntimeError(
            f"{self._NAME} event publisher is not configured — set {self._ENV} and install its "
            f"client library, or use EXAMLOPS_EVENT_PUBLISHER=log. The outbox row is retained "
            f"(not dropped) so no event is lost."
        )


class NatsPublisher(_BrokerSkeleton):
    _NAME = "nats"
    _ENV = "EXAMLOPS_NATS_URL"


class KafkaPublisher(_BrokerSkeleton):
    _NAME = "kafka"
    _ENV = "EXAMLOPS_KAFKA_BROKERS"


class RedisStreamsPublisher:
    """Publish durable event envelopes to a Redis Stream.

    Redis assigns its own ordered stream entry ID; ``event_id`` is the stable outbox identifier
    used by consumers for deduplication if a relay crashes after ``XADD`` but before acknowledging
    the database row.
    """

    def __init__(self, client: Any | None = None) -> None:
        self._stream = os.getenv("EXAMLOPS_REDIS_EVENT_STREAM", "examlops.events").strip()
        if not self._stream:
            raise RuntimeError("EXAMLOPS_REDIS_EVENT_STREAM must not be empty")
        maxlen = os.getenv("EXAMLOPS_REDIS_EVENT_MAXLEN", "100000").strip()
        try:
            self._maxlen = int(maxlen)
        except ValueError as exc:
            raise RuntimeError("EXAMLOPS_REDIS_EVENT_MAXLEN must be an integer") from exc
        if self._maxlen <= 0:
            raise RuntimeError("EXAMLOPS_REDIS_EVENT_MAXLEN must be greater than zero")
        if client is not None:
            self._client = client
            return

        url = os.getenv("EXAMLOPS_REDIS_URL", "").strip()
        if not url:
            raise RuntimeError(
                "redis event publisher is not configured — set EXAMLOPS_REDIS_URL or use "
                "EXAMLOPS_EVENT_PUBLISHER=log"
            )
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError(
                "redis event publisher requires the 'redis' package; install "
                "'examlops[coordination]'"
            ) from exc
        self._client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )

    def publish(self, topic: str, payload: dict[str, Any], *, event_id: str) -> None:
        self._client.xadd(
            self._stream,
            {
                "event_id": event_id,
                "topic": topic,
                "payload": json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
            },
            maxlen=self._maxlen,
            approximate=True,
        )


_PUBLISHERS: dict[str, type] = {
    "log": LogPublisher,
    "nats": NatsPublisher,
    "kafka": KafkaPublisher,
    "redis": RedisStreamsPublisher,
}

_publisher: EventPublisher | None = None


def get_publisher() -> EventPublisher:
    """Resolve the configured publisher (``EXAMLOPS_EVENT_PUBLISHER``, default ``log``). Cached."""
    global _publisher
    if _publisher is None:
        name = os.getenv("EXAMLOPS_EVENT_PUBLISHER", "log").strip().lower()
        cls = _PUBLISHERS.get(name)
        if cls is None:
            supported = ", ".join(sorted(_PUBLISHERS))
            raise RuntimeError(
                f"unsupported EXAMLOPS_EVENT_PUBLISHER={name!r}; expected one of: {supported}"
            )
        _publisher = cls()
    return _publisher


def reset_publisher() -> None:
    """Clear the cached publisher (tests / after an env change)."""
    global _publisher
    _publisher = None


def publish(topic: str, payload: dict[str, Any]) -> int:
    """Durably enqueue an event to the outbox (does NOT publish inline). Returns its row id.

    The relay does the actual broker publish, so a broker outage never blocks the domain write.
    """
    from examlops.data import init_db
    from examlops.data.events import enqueue_event

    init_db()
    return enqueue_event(topic, payload)


def relay_once(limit: int = 100) -> dict[str, int]:
    """Publish one batch of outbox events via the configured publisher (item 1.3).

    Claims up to ``limit`` unpublished rows atomically, publishes each, and marks it
    published/failed. Returns ``{"published": n, "failed": m, "claimed": k}``. Idempotent and
    safe to run concurrently (the claim bumps attempts under a write lock) or on a timer/cron.
    """
    from examlops.data import init_db
    from examlops.data.events import claim_outbox_batch, mark_event_failed, mark_event_published

    init_db()
    publisher = get_publisher()
    attempts_text = os.getenv("EXAMLOPS_EVENT_MAX_ATTEMPTS", "5").strip()
    try:
        max_attempts = int(attempts_text)
    except ValueError as exc:
        raise RuntimeError("EXAMLOPS_EVENT_MAX_ATTEMPTS must be an integer") from exc
    if max_attempts <= 0:
        raise RuntimeError("EXAMLOPS_EVENT_MAX_ATTEMPTS must be greater than zero")
    batch = claim_outbox_batch(limit, max_attempts=max_attempts)
    published = failed = 0
    for row in batch:
        try:
            payload = json.loads(row["payload"])
            publisher.publish(row["topic"], payload, event_id=f"outbox:{row['id']}")
            mark_event_published(row["id"])
            published += 1
        except Exception as exc:  # noqa: BLE001 - relay must not crash on one bad event
            mark_event_failed(row["id"], str(exc))
            failed += 1
            logger.warning(
                "event relay failed for id=%s topic=%s: %s", row["id"], row["topic"], exc
            )
    return {"claimed": len(batch), "published": published, "failed": failed}
