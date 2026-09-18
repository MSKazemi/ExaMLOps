"""NATS JetStream transport for the event backbone (ADR 0124).

`nats-py` is asyncio-only while the relay, the CLI and the service workers are synchronous, so a
single background event loop (one daemon thread per process) owns the connection and every call
crosses into it with ``run_coroutine_threadsafe``. The connection reconnects on its own; a call that
cannot complete within its timeout raises, and the outbox keeps the event for the next relay pass.

Configuration (all optional except the URL):

    EXAMLOPS_NATS_URL                  nats://host:4222 (comma-separated for a cluster)
    EXAMLOPS_NATS_STREAM               EXAMLOPS_EVENTS
    EXAMLOPS_NATS_SUBJECT_PREFIX       examlops.events
    EXAMLOPS_NATS_MAX_AGE_SECONDS      604800 (7 days of retention)
    EXAMLOPS_NATS_DUPLICATE_WINDOW     120 (seconds of broker-side dedup on Nats-Msg-Id)
    EXAMLOPS_NATS_REPLICAS             1 (3 on a clustered deployment)
    EXAMLOPS_NATS_TIMEOUT              5 (seconds per operation)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)

DEFAULT_STREAM = "EXAMLOPS_EVENTS"
DEFAULT_PREFIX = "examlops.events"
DLQ_PREFIX = "examlops.dlq"
_SUBJECT_TOKEN = re.compile(r"[^A-Za-z0-9_.\-]")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc


def is_unavailable(exc: BaseException) -> bool:
    """Is this `nats-py` failure the connection, rather than the message being refused?

    The relay stops a batch on the first of these instead of paying ``EXAMLOPS_NATS_TIMEOUT``
    again for every remaining event, and keeps their retry budget for events the broker really
    did refuse. Classified by the client's own error classes where they exist — the timeout that
    :class:`_LoopThread` raises is the builtin one, because the future, not NATS, gave up.
    """
    if isinstance(exc, TimeoutError | ConnectionError | OSError):
        return True
    try:  # nats-py is optional: without it there is no publisher to classify for
        from nats import errors as nats_errors  # noqa: PLC0415
        from nats.js import errors as js_errors  # noqa: PLC0415
    except ImportError:  # pragma: no cover - the publisher could not have been constructed
        return False
    return isinstance(
        exc,
        nats_errors.NoServersError
        | nats_errors.ConnectionClosedError
        | nats_errors.StaleConnectionError
        | nats_errors.OutboundBufferLimitError
        | nats_errors.TimeoutError
        | js_errors.NoStreamResponseError
        | js_errors.ServiceUnavailableError,
    )


def subject_for(topic: str, prefix: str | None = None) -> str:
    """``retrain.run_completed`` → ``examlops.events.retrain.run_completed`` (unsafe chars → ``_``)."""
    base = prefix or os.getenv("EXAMLOPS_NATS_SUBJECT_PREFIX", DEFAULT_PREFIX).strip()
    token = _SUBJECT_TOKEN.sub("_", topic.strip()).strip(".") or "unnamed"
    return f"{base}.{token}"


class _LoopThread:
    """One asyncio loop in a daemon thread; sync callers submit coroutines to it."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="nats-loop", daemon=True)
        self._thread.start()

    def run(self, coro: Coroutine[Any, Any, T], timeout: float) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)


class JetStream:
    """A connected JetStream context that can publish, ensure the stream and pull-consume."""

    def __init__(self, url: str | None = None) -> None:
        try:
            import nats  # noqa: F401,PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "the NATS event backbone needs the 'nats-py' package; install 'examlops[events]'"
            ) from exc
        self.url = (url or os.getenv("EXAMLOPS_NATS_URL") or "").strip()
        if not self.url:
            raise RuntimeError(
                "NATS event backbone is not configured — set EXAMLOPS_NATS_URL "
                "(or use EXAMLOPS_EVENT_PUBLISHER=log)"
            )
        self.stream = os.getenv("EXAMLOPS_NATS_STREAM", DEFAULT_STREAM).strip() or DEFAULT_STREAM
        self.prefix = (
            os.getenv("EXAMLOPS_NATS_SUBJECT_PREFIX", DEFAULT_PREFIX).strip() or DEFAULT_PREFIX
        )
        self.timeout = _env_float("EXAMLOPS_NATS_TIMEOUT", 5.0)
        self._loop = _LoopThread()
        self._nc: Any = None
        self._js: Any = None
        self._lock = threading.Lock()
        self._stream_ready = False
        # One pull subscription per durable consumer, reused across fetches. Creating one per fetch
        # leaked a subscription (and its inbox) on every poll of a long-running consumer.
        self._subs: dict[tuple[str, str], Any] = {}

    # -- connection -----------------------------------------------------------------------
    async def _connect(self) -> Any:
        import nats  # noqa: PLC0415

        if self._nc is None or self._nc.is_closed:
            self._nc = await nats.connect(
                servers=[s.strip() for s in self.url.split(",") if s.strip()],
                connect_timeout=self.timeout,
                max_reconnect_attempts=-1,
                name=f"examlops-{os.getpid()}",
            )
            self._js = self._nc.jetstream(timeout=self.timeout)
        return self._js

    async def _ensure_stream(self) -> None:
        from nats.js.api import StorageType, StreamConfig  # noqa: PLC0415
        from nats.js.errors import NotFoundError  # noqa: PLC0415

        js = await self._connect()
        try:
            await js.stream_info(self.stream)
        except NotFoundError:
            await js.add_stream(
                StreamConfig(
                    name=self.stream,
                    subjects=[f"{self.prefix}.>", f"{DLQ_PREFIX}.>"],
                    storage=StorageType.FILE,
                    max_age=_env_float("EXAMLOPS_NATS_MAX_AGE_SECONDS", 7 * 24 * 3600.0),
                    duplicate_window=_env_float("EXAMLOPS_NATS_DUPLICATE_WINDOW", 120.0),
                    num_replicas=int(_env_float("EXAMLOPS_NATS_REPLICAS", 1)),
                )
            )
        self._stream_ready = True

    def ensure_stream(self) -> None:
        with self._lock:
            if not self._stream_ready:
                self._loop.run(self._ensure_stream(), self.timeout * 2)

    async def _check(self) -> None:
        js = await self._connect()
        await js.account_info()  # the broker answered, not just the TCP socket

    def check(self) -> None:
        """Raise unless the broker is reachable and answering, within ``EXAMLOPS_NATS_TIMEOUT``.

        What the control plane's startup check calls. Constructing a publisher proves only that the
        client library is installed: the backbone chaos drill killed NATS and `/health` still said
        the publisher was fine, while the outbox quietly filled up.
        """
        self._loop.run(self._check(), self.timeout)

    # -- publish --------------------------------------------------------------------------
    async def _publish(self, subject: str, body: bytes, msg_id: str) -> bool:
        js = await self._connect()
        ack = await js.publish(subject, body, headers={"Nats-Msg-Id": msg_id})
        return bool(getattr(ack, "duplicate", False))

    def publish(self, subject: str, body: bytes, *, msg_id: str) -> bool:
        """Publish once; returns True when JetStream recognised ``msg_id`` as a duplicate."""
        self.ensure_stream()
        return self._loop.run(self._publish(subject, body, msg_id), self.timeout)

    # -- consume --------------------------------------------------------------------------
    async def _pull(
        self, durable: str, filter_subject: str, batch: int, wait: float, max_deliver: int
    ) -> list[Any]:
        from nats.errors import TimeoutError as NatsTimeout  # noqa: PLC0415
        from nats.js.api import AckPolicy, ConsumerConfig  # noqa: PLC0415

        js = await self._connect()
        key = (durable, filter_subject)
        sub = self._subs.get(key)
        if sub is None:
            sub = await js.pull_subscribe(
                filter_subject,
                durable=durable,
                stream=self.stream,
                config=ConsumerConfig(
                    ack_policy=AckPolicy.EXPLICIT,
                    max_deliver=max_deliver,
                    ack_wait=max(30.0, wait * 4),
                ),
            )
            self._subs[key] = sub
        try:
            return await sub.fetch(batch, timeout=wait)
        except NatsTimeout:
            return []

    def fetch(
        self, durable: str, filter_subject: str, *, batch: int, wait: float, max_deliver: int
    ) -> list[Any]:
        self.ensure_stream()
        return self._loop.run(
            self._pull(durable, filter_subject, batch, wait, max_deliver), wait + self.timeout
        )

    async def _recent(self, filter_subject: str, limit: int) -> list[Any]:
        from nats.errors import TimeoutError as NatsTimeout  # noqa: PLC0415
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy  # noqa: PLC0415

        js = await self._connect()
        info = await js.stream_info(self.stream)
        start = max(1, int(info.state.last_seq) - max(1, limit) * 20 + 1)
        # An ephemeral, never-acknowledging consumer: reading the tail leaves no durable state.
        sub = await js.pull_subscribe(
            filter_subject,
            stream=self.stream,
            config=ConsumerConfig(
                ack_policy=AckPolicy.NONE,
                deliver_policy=DeliverPolicy.BY_START_SEQUENCE,
                opt_start_seq=start,
                inactive_threshold=10.0,
            ),
        )
        messages: list[Any] = []
        try:
            while True:
                try:
                    batch = await sub.fetch(100, timeout=0.5)
                except NatsTimeout:
                    break
                messages.extend(batch)
        finally:
            await sub.unsubscribe()
        return messages[-limit:]

    def recent(self, filter_subject: str | None = None, *, limit: int = 20) -> list[Any]:
        """The last ``limit`` messages on ``filter_subject`` (default: every event), oldest first."""
        self.ensure_stream()
        subject = filter_subject or f"{self.prefix}.>"
        return self._loop.run(self._recent(subject, limit), self.timeout * 4)

    async def _backbone_stats(self) -> dict[str, dict[str, Any]]:
        js = await self._connect()
        consumers: dict[str, dict[str, int]] = {}
        for info in await js.consumers_info(self.stream):
            durable = getattr(info.config, "durable_name", None)
            if not durable:  # an ephemeral reader (exa events tail) is not a consumer to watch
                continue
            consumers[durable] = {
                "pending": int(info.num_pending or 0),
                "ack_pending": int(info.num_ack_pending or 0),
                "redelivered": int(info.num_redelivered or 0),
            }
        parked = await js.stream_info(self.stream, subjects_filter=f"{DLQ_PREFIX}.>")
        dlq = {
            subject[len(DLQ_PREFIX) + 1 :]: int(count)
            for subject, count in (parked.state.subjects or {}).items()
        }
        return {"consumers": consumers, "dlq": dlq}

    def backbone_stats(self) -> dict[str, dict[str, Any]]:
        """Per durable consumer: events not yet delivered (``pending``), delivered but not yet
        acknowledged (``ack_pending``) and redelivered; per consumer, events parked on its
        dead-letter subject within the stream's retention (``dlq``)."""
        self.ensure_stream()
        return self._loop.run(self._backbone_stats(), self.timeout * 2)

    # -- broadcast ------------------------------------------------------------------------
    async def _watch(self, filter_subject: str, callback: Callable[[bytes], None]) -> Any:
        from nats.js.api import DeliverPolicy  # noqa: PLC0415

        js = await self._connect()

        async def _on_message(msg: Any) -> None:
            try:
                callback(bytes(msg.data))
            except Exception as exc:  # noqa: BLE001 - one bad handler call must not kill the feed
                logger.warning("broadcast handler failed on %s: %s", msg.subject, exc)

        # An ordered consumer is ephemeral and needs no acknowledgements: exactly the shape of
        # "every copy of this process sees every new event", with no durable state left behind
        # when the process goes away.
        return await js.subscribe(
            filter_subject,
            cb=_on_message,
            stream=self.stream,
            ordered_consumer=True,
            deliver_policy=DeliverPolicy.NEW,
        )

    def watch(
        self, filter_subject: str | None, callback: Callable[[bytes], None]
    ) -> Callable[[], None]:
        """Call ``callback(body)`` for every **new** event on ``filter_subject``, in this process.

        A broadcast, not a work queue: every process that watches gets every event, and nothing is
        acknowledged or remembered — for fan-out to live views (each dashboard replica's SSE), not
        for effects, which belong in a durable :class:`EventConsumer`. The callback runs on the
        connection's loop thread and must hand off quickly. Returns a function that stops the feed.
        """
        self.ensure_stream()
        subject = filter_subject or f"{self.prefix}.>"
        sub = self._loop.run(self._watch(subject, callback), self.timeout * 2)

        def _stop() -> None:
            try:
                self._loop.run(sub.unsubscribe(), self.timeout)
            except Exception as exc:  # noqa: BLE001 - best effort on shutdown
                logger.debug("broadcast unsubscribe failed: %s", exc)

        return _stop

    # -- key-value (the serving snapshot, ADR 0127) ------------------------------------------
    async def _bucket(self, bucket: str) -> Any:
        from nats.js.api import KeyValueConfig, StorageType  # noqa: PLC0415
        from nats.js.errors import BucketNotFoundError  # noqa: PLC0415

        js = await self._connect()
        try:
            return await js.key_value(bucket)
        except BucketNotFoundError:
            return await js.create_key_value(
                config=KeyValueConfig(
                    bucket=bucket,
                    history=5,
                    storage=StorageType.FILE,
                    replicas=int(_env_float("EXAMLOPS_NATS_REPLICAS", 1)),
                )
            )

    async def _kv_put(self, bucket: str, key: str, value: bytes) -> int:
        return int(await (await self._bucket(bucket)).put(key, value))

    async def _kv_get(self, bucket: str, key: str) -> tuple[bytes, int] | None:
        from nats.js.errors import KeyNotFoundError  # noqa: PLC0415

        try:
            entry = await (await self._bucket(bucket)).get(key)
        except KeyNotFoundError:
            return None
        return (bytes(entry.value or b""), int(entry.revision or 0))

    def kv_put(self, bucket: str, key: str, value: bytes) -> int:
        """Store ``value`` under ``key`` (bucket created on first use); returns the revision."""
        return self._loop.run(self._kv_put(bucket, key, value), self.timeout)

    def kv_get(self, bucket: str, key: str) -> tuple[bytes, int] | None:
        """``(value, revision)``, or ``None`` when the key has never been written."""
        return self._loop.run(self._kv_get(bucket, key), self.timeout)

    def settle(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run a message's ack/nak/term coroutine on the connection's loop."""
        return self._loop.run(coro, self.timeout)

    async def _unsubscribe_all(self) -> None:
        subs, self._subs = list(self._subs.values()), {}
        for sub in subs:
            try:
                await sub.unsubscribe()
            except Exception:  # noqa: BLE001 - best effort on shutdown
                pass

    def close(self) -> None:
        if self._nc is not None and not self._nc.is_closed:
            self._loop.run(self._unsubscribe_all(), self.timeout)
            self._loop.run(self._nc.drain(), self.timeout)


_shared: JetStream | None = None
_shared_lock = threading.Lock()


def shared() -> JetStream:
    """The process-wide JetStream connection (one loop thread, one socket)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = JetStream()
        return _shared


def reset_shared() -> None:
    global _shared
    with _shared_lock:
        _shared = None
