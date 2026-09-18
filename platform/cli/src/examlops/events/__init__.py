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
    dependency-free (works offline, in tests, single-node dev); ``nats`` publishes to NATS
    JetStream (ADR 0124, idempotent at the broker via ``Nats-Msg-Id``); ``redis`` appends to a
    Redis Stream. ``kafka`` is a placeholder that fails loudly instead of silently dropping events.
  * every event leaves as a CloudEvents 1.0 envelope (:mod:`examlops.events.envelope`); a publisher
    that implements ``publish_event(envelope)`` receives the whole envelope from the relay.
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

    def is_unavailable(self, exc: BaseException) -> bool:
        """Always: nothing here can publish, and no event is to blame for that. The outbox keeps
        the backlog visible and drainable instead of poisoning it over a misconfiguration."""
        return True


class NatsPublisher:
    """Publish CloudEvents to NATS JetStream (ADR 0124).

    The subject is ``<prefix>.<topic>`` (default ``examlops.events.retrain.scheduled``) and the
    ``Nats-Msg-Id`` header is the stable outbox id, so a relay that publishes and then crashes
    before marking the row produces one stream message, not two, within the duplicate window.
    """

    def __init__(self, stream: Any | None = None) -> None:
        from examlops.events import nats_backend  # noqa: PLC0415

        self._js = stream if stream is not None else nats_backend.shared()
        self._subject_for = nats_backend.subject_for

    def check(self) -> None:
        """Raise unless the broker is reachable (the control plane's startup check calls this)."""
        self._js.check()

    def is_unavailable(self, exc: BaseException) -> bool:
        """Did this failure come from the connection rather than the event? `nats-py` answers."""
        from examlops.events import nats_backend  # noqa: PLC0415

        return nats_backend.is_unavailable(exc)

    def publish(self, topic: str, payload: dict[str, Any], *, event_id: str) -> None:
        from examlops.events import envelope  # noqa: PLC0415

        self.publish_event(envelope.build(topic, payload, event_id=event_id))

    def publish_event(self, event: dict[str, Any]) -> None:
        from examlops.events import envelope  # noqa: PLC0415

        subject = self._subject_for(envelope.topic_of(event))
        self._js.publish(subject, envelope.encode(event), msg_id=str(event["id"]))


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


def publish(
    topic: str,
    payload: dict[str, Any],
    *,
    actor: str | None = None,
    tenant: str | None = None,
) -> int:
    """Durably enqueue an event to the outbox (does NOT publish inline). Returns its row id.

    The relay does the actual broker publish, so a broker outage never blocks the domain write.
    """
    from examlops.data import init_db
    from examlops.data.events import enqueue_event

    init_db()
    return enqueue_event(topic, payload, actor=actor, tenant=tenant)


def alias_changed(
    model: str,
    alias: str,
    version: str | int | None,
    *,
    previous_version: str | int | None = None,
    removed: bool = False,
    actor: str | None = None,
    via: str = "cli",
) -> int | None:
    """Announce that an MLflow alias moved: ``model.alias_changed`` (P2.4).

    Every surface that moves an alias — ``exa pipeline promote``/``rollback``/``production``, the
    autopilot, the training pipeline, the agent, the dashboard — calls this after MLflow accepted
    the change, so the serving plane (ADR 0127) can react to the change instead of polling for it.

    MLflow is the alias's system of record and has already committed, so this cannot share its
    transaction. A failed enqueue is logged and swallowed rather than failing a promotion that
    did happen: the serving plane's alias poll (``RAY_RELOAD_POLL_SECONDS``) stays as the
    backstop for a lost event. Returns the outbox id, or ``None`` when the enqueue failed.
    """
    payload = {
        "model": model,
        "model_key": model.strip().lower(),
        "alias": alias,
        "version": None if version is None else str(version),
        "previous_version": None if previous_version is None else str(previous_version),
        "removed": removed,
        "via": via,
    }
    try:
        return publish(
            "model.alias_changed",
            payload,
            actor=actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or None,
        )
    except Exception as exc:  # noqa: BLE001 - the alias moved; losing its event must not undo that
        logger.warning("model.alias_changed for %s/%s not enqueued: %s", model, alias, exc)
        return None


# Signs, in an exception's text, that the *transport* failed rather than the event being refused.
# Deliberately narrow: a message that merely says something broke ("broker down") is not evidence
# about the connection, and treating it as one would let a genuinely poison event retry forever.
_UNAVAILABLE_SIGNS = (
    "no servers",
    "connection refused",
    "connection closed",
    "connection reset",
    "connection lost",
    "no route to host",
    "name or service not known",
    "temporary failure in name resolution",
    "timed out",
    "timeout",
    "unreachable",
    "broken pipe",
    "network is down",
)


def describe(exc: BaseException) -> str:
    """A never-empty one-line description of a publish failure.

    ``str(exc)`` is empty for some of the exceptions that matter most here — a bare
    ``TimeoutError`` is what :class:`examlops.events.nats_backend._LoopThread` raises when the
    broker does not answer within ``EXAMLOPS_NATS_TIMEOUT``. An empty reason read as *no* reason:
    the relay reported ``unavailable: ""``, the control plane's falsy check dropped it, and
    `/health` said ``ok`` through the outage this whole path exists to make visible.
    """
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def is_unavailable(publisher: Any, exc: BaseException) -> bool:
    """Is this failure "the backbone is not there" rather than "this event was refused"?

    A publisher that understands its own client library answers for itself (``is_unavailable``);
    otherwise the exception type and text decide. The distinction is what separates a backlog that
    drains by itself when the broker returns from an event stranded as poison: see
    :func:`examlops.data.events.defer_outbox_claim`.
    """
    own = getattr(publisher, "is_unavailable", None)
    if callable(own):
        try:
            return bool(own(exc))
        except Exception as verdict_failed:  # noqa: BLE001 - a broken classifier decides nothing
            logger.warning(
                "%s could not classify %r: %s", type(publisher).__name__, exc, verdict_failed
            )
    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    text = str(exc).lower()
    return any(sign in text for sign in _UNAVAILABLE_SIGNS)


def relay_once(limit: int = 100) -> dict[str, Any]:
    """Publish one batch of outbox events via the configured publisher (item 1.3).

    Claims up to ``limit`` unpublished rows atomically, publishes each, and marks it
    published/failed. Returns ``{"published": n, "failed": m, "claimed": k}``. Idempotent and
    safe to run concurrently (the claim bumps attempts under a write lock) or on a timer/cron.

    A failure that means *the backbone is not there* (:func:`is_unavailable`) ends the batch: the
    rows still in it are deferred, with no attempt charged, and the result carries ``deferred``
    and ``unavailable``. Without that, a cycle against a dead broker costs one client timeout per
    event — 36 seconds for six events in the chaos drill, over eight minutes at the default batch
    size — during which the relay makes no progress and ``/health`` still shows the previous
    cycle's verdict.
    """
    from examlops.data import init_db
    from examlops.data.events import (
        claim_outbox_batch,
        defer_outbox_claim,
        mark_event_failed,
        mark_event_published,
    )

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
    unavailable: str | None = None
    deferred: list[int] = []
    from examlops.events import envelope  # noqa: PLC0415

    for index, row in enumerate(batch):
        try:
            publish_event = getattr(publisher, "publish_event", None)
            if callable(publish_event):
                publish_event(envelope.from_outbox_row(row))
            else:
                payload = json.loads(row["payload"])
                publisher.publish(row["topic"], payload, event_id=f"outbox:{row['id']}")
            mark_event_published(row["id"])
            published += 1
        except Exception as exc:  # noqa: BLE001 - relay must not crash on one bad event
            if is_unavailable(publisher, exc):
                # Nowhere to send the rest either. Stop here rather than proving it once per row.
                unavailable = describe(exc)
                deferred = [r["id"] for r in batch[index:]]
                defer_outbox_claim(deferred, unavailable)
                logger.warning(
                    "event backbone unavailable (%s); %d event(s) stay in the outbox",
                    unavailable,
                    len(deferred),
                )
                break
            mark_event_failed(row["id"], describe(exc))
            failed += 1
            logger.warning(
                "event relay failed for id=%s topic=%s: %s",
                row["id"],
                row["topic"],
                describe(exc),
            )
    result: dict[str, Any] = {"claimed": len(batch), "published": published, "failed": failed}
    if unavailable is not None:
        # Only when it happened: callers sum the three counts, and `/health` reads `unavailable`.
        result["deferred"] = len(deferred)
        result["unavailable"] = unavailable
    return result
