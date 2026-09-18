"""Kafka stream connector — at-least-once live inference over a consumer group (ADR 0131, E5).

One :class:`KafkaStreamSession` per running stream: a consumer-group member on the binding's topic
(``binding.address``) that feeds every message through :class:`StreamIngress` on a bounded worker
pool, answers on ``options.reply_topic`` and parks what it cannot process through a
:class:`~examlops.dataplane.streams.dlq.DeadLetterSink` (plus ``options.dlq_topic`` when set).

**The invariant.** No offset is stored while any lower offset received on that partition has not
reached a *terminal* result: answered (``ok``/``model``, its reply delivered), or dead-lettered
(the sink record written, and its ``dlq_topic`` copy and failure reply delivered when those are
configured). A failed attempt is never terminal.

* ``enable.auto.offset.store=false`` and ``enable.auto.commit=true``: the loop *stores* offsets and
  librdkafka commits the stored ones on its usual interval; the loop also commits explicitly
  inside ``on_revoke`` (before the partitions go) and on shutdown.
* Per partition, every received offset stays in ``outstanding`` until it is terminal — whether it
  is queued, in flight, or *awaiting re-fetch* (``refetch``: failed and backing off, purged from
  the local backlog, or dropped because it arrived while its partition was paused). The store
  position is ``min(outstanding ∪ {high, seek target})``, ``high`` being one past the highest
  offset received. Workers finish out of order: completing 5, 7, 6 stores 6, then 8. Offset gaps
  (compaction, transaction markers) do not stall it: only received offsets count.
* ``high`` never moves back, so *terminal* needs no bookkeeping of its own: a redelivered offset
  below ``high`` that is not outstanding is terminal (or a gap the broker never delivers). Per
  partition the connector keeps only ``outstanding`` — in flight (≤ pool), queued (≤ backlog) and
  parked offsets — so memory stays bounded however long a head-of-line message backs off.
* **Seeking** always targets the *lowest* offset awaiting re-fetch, recomputed at every seek, and
  only when that offset lies behind the consumer's fetch position — never forward past a pending
  offset. Redelivered offsets that are already terminal, queued or in flight are skipped; anything
  else not terminal is processed.
* **Gone offsets.** A parked offset can leave the log while it waits (retention, compaction,
  DeleteRecords). Kafka delivers in order from a sought position, so when the first message after
  a seek to ``t`` is at ``o > t``, every parked offset in ``[t, o)`` is gone: each becomes terminal
  as ``expired`` — a dead letter ``expired_from_log`` (no payload; origin topic/partition/offset),
  an ``ok:false`` reply with outcome ``expired`` when ``reply_topic`` is set, the counter
  ``dataplane_stream_messages_expired_total``, one WARNING per detection (never a payload) — and
  the partition moves on. librdkafka's explicit out-of-range error (``OFFSET_OUT_OF_RANGE``, or
  ``_AUTO_OFFSET_RESET``) on a partition with parked offsets arms the same check and seeks to the
  log start. A parked offset that already owed a dead letter (its sink record written, its dlq
  copy or reply still failing) keeps that dead letter: only its missing parts are finished,
  value-less, and it is not re-labelled ``expired``.

  *Known window (document-only, ruling R22-4).* An ``expired`` dead letter still owed when its
  partition is revoked or the stream stops (its write failing and backing off, or the pool full)
  is dropped with the partition state. Nothing was stored past it, so the next owner seeks to the
  committed offset, finds the message gone in the same way, and derives the ``expired`` dead
  letter again; only the *reason* of an offset whose original dead letter was part-written is
  lost in that hand-over (the next owner writes ``expired_from_log``).
* **Where a partition starts.** ``auto.offset.reset`` is always ``earliest``: it also decides
  where an out-of-range position resumes, and ``latest`` there would jump over messages still in
  the log — skipped offsets are never received, so nothing would ever record them. The operator
  choice ``options.start`` (``earliest`` by default, or ``latest``) applies only to a partition
  the group has **no committed offset** for, on its first assignment: with ``latest`` it is
  positioned at the high watermark before its first fetch (one INFO line says so), so a new
  stream on a long topic does not replay its history. A partition with a committed offset always
  resumes from it, whatever ``start`` says; one whose committed offset cannot be read (a
  per-partition error) starts as ``earliest`` would — a replay, never a skip.

  *Known window (document-only, ruling R22-4).* The ``latest`` start is *stored*, then committed
  by auto-commit (≤ 5 s) or the next revoke. If the member is lost uncleanly before that first
  commit, the next owner again sees "no committed offset": it starts at the then-current log
  end, skipping what was produced in between, and anything stored-but-uncommitted by the lost
  member is redelivered. Once a partition has one commit, ``start`` never applies to it again.

**Per-partition state machine** (each offset: ``queued → in_flight → terminal``, or
``in_flight → refetch → queued …``)::

    flowing ──retryable failure / dead-letter write failed──▶ backing-off
       ▲         (offset → refetch; the partition's queued offsets → refetch; paused until
       │          Retry-After, or full-jitter backoff 0.5 s·2ⁿ⁻¹ ≤ 30 s)
       └──── seek(min(refetch)) when behind the fetch position → resume ◀── deadline passed

Orthogonal pauses: the whole stream (:meth:`KafkaStreamSession.set_paused`, A8b), a full local
backlog (flow control), and the drain on stop. A message that arrives
from a paused partition goes to ``refetch`` and is fetched again after the seek on resume.

**Outcomes.**

* ``ok``/``model`` → reply (``ok:true`` / ``ok:false``) when ``reply_topic`` is set; terminal once
  it is delivered. A reply that fails delivery, or cannot be produced, is retried like
  ``transport``.
* ``transport``, ``deadline``, and any ``overloaded`` that is not the ingress's own shed → back
  off and retry; each spends an attempt, counted per (partition, offset). At
  ``limits.max_attempts`` the message is dead-lettered (``retries_exhausted``).
* An ingress shed — ``IngressResult.shed_reason`` ``"in_flight"`` (its permits were all taken)
  or ``"rate"`` (the stream's own ``rate_per_min``), :func:`is_local_shed` — is backpressure: the
  partition waits ``retry_after`` and the attempt is **not** spent, so a backlog replay under a
  low rate never dead-letters.
* ``validation``, ``not_found``, ``unexpected``, an oversize value (checked on the raw bytes before
  any parse), a value that is not JSON, or a malformed envelope → dead-lettered at once.
* A dead letter is: the sink record, then (when configured) the raw copy to ``dlq_topic`` and an
  ``ok:false`` reply. If any part fails — ``record()`` raises, or a delivery fails — the offset is
  not stored; after a backoff only the missing parts are redone (inference is never re-run).
  These retries are unbounded: a dead letter is never swallowed.

**Replies** (R15): every terminal outcome gets one when ``reply_topic`` is set —
``{"ok", "outcome", "prediction", "model", "version", "error"}``, keyed by the request's key,
``traceparent`` propagated, ``error`` redacted and ≤ 512 bytes. ``outcome`` is the ingress outcome
(``ok``, ``model``, ``validation``, ``not_found``, ``unexpected``), ``validation`` for a message
rejected before inference (the error names the reason: ``oversize``, ``not_json``,
``invalid_message``), or ``retries_exhausted``.

**Message format.** The value is a JSON object. If its top level has a ``payload`` key whose
value is an object, it is an *envelope* — ``{"payload": {...}, "model"?: str, "alias"?: str,
"metadata"?: {...}}``, other top-level keys ignored; otherwise the whole object is the payload. A
payload that itself has an object-valued ``payload`` field must therefore be sent in an envelope.
The message key becomes ``metadata.key`` and, unless the envelope's metadata sets one,
``metadata.job_id``; a message can never set ``metadata.tenant``. Headers: ``traceparent`` (W3C;
ignored when malformed) and ``idempotency-key`` (≤ 200 characters of UTF-8, else dead-lettered).

**Threads.** Only the loop thread touches the consumer and the partition state. Workers run
``handle()``, the sink and produce; their verdicts and every delivery report reach the loop
through one queue. :meth:`KafkaStreamSession.set_paused` only flips a lock-guarded flag the loop
applies on its next iteration, so A8b may call it from any thread. The loop never waits on a
worker: it keeps calling ``poll()`` (the group heartbeat and rebalance callbacks) regardless.

Security: the connection comes from a ``kafka`` Named Connection through Plan 1's conf builder,
which egress-checks every bootstrap host; payloads are never logged; every error text leaving the
connector (a reply, a dead-letter header, a status detail, a log line) is redacted and bounded.
"""

from __future__ import annotations

import itertools
import json
import logging
import random
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from queue import Empty, SimpleQueue
from typing import TYPE_CHECKING, Any, Protocol

from examlops.dataplane.connectors import kafka as _kafka
from examlops.dataplane.streams import metrics
from examlops.dataplane.streams.client import RETRY_AFTER_MAX_S
from examlops.dataplane.streams.dlq import (
    REASON_INVALID_MESSAGE,
    REASON_NOT_JSON,
    REASON_OVERSIZE,
    REASON_RETRIES_EXHAUSTED,
    DeadLetterSink,
    LoggingDeadLetterSink,
    redact_error,
)
from examlops.dataplane.streams.ingress import LOCAL_SHED_REASONS as _LOCAL_SHED_REASONS
from examlops.dataplane.streams.ingress import is_local_shed as _is_local_shed
from examlops.dataplane.streams.types import (
    InferenceResult,
    StreamBinding,
    StreamRequest,
    display_project,
)
from examlops.dataplane.types import DataplaneError, SpecError

if TYPE_CHECKING:
    from examlops.dataplane.streams.connectors import StatusCallback
    from examlops.dataplane.streams.ingress import IngressResult, StreamIngress

logger = logging.getLogger(__name__)

#: Ceiling on the worker pool, whatever ``limits.max_in_flight`` says.
MAX_WORKERS = 32
#: Full-jitter exponential backoff between retries when no ``Retry-After`` is given.
BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 30.0
DEFAULT_POLL_TIMEOUT_S = 0.2
#: How long a stop waits for in-flight messages and deliveries before committing anyway.
DEFAULT_DRAIN_TIMEOUT_S = 10.0
#: The wait after an ingress shed that carried no ``Retry-After``.
LOCAL_SHED_RETRY_AFTER_S = 1.0
IDEMPOTENCY_KEY_MAX = 200
#: Outcomes that are an answer: replied to, then terminal.
ANSWERED = frozenset({"ok", "model"})
#: Outcomes retried with backoff; the model service could not answer this time.
RETRYABLE = frozenset({"transport", "overloaded", "deadline"})
#: The headers on a dead letter produced to ``dlq_topic`` (values redacted, ≤ 512 B of error).
DLQ_HEADERS = ("x-examlops-error", "x-examlops-stream", "x-examlops-attempts", "x-examlops-origin")
#: The reply ``outcome`` of a message rejected before inference, and of an exhausted retry.
REJECTED_OUTCOME = "validation"
EXHAUSTED_OUTCOME = "retries_exhausted"
#: The dead-letter reason and reply outcome of a parked message that left the log.
REASON_EXPIRED = "expired_from_log"
EXPIRED_OUTCOME = "expired"
#: ``options.start`` — where a partition with no committed offset starts (module docstring).
START_POSITIONS = ("earliest", "latest")
_OFFSET_INVALID = -1001  # librdkafka RD_KAFKA_OFFSET_INVALID: "no committed offset" / use it

_PARTITION_EOF = -191  # librdkafka RD_KAFKA_RESP_ERR__PARTITION_EOF
#: librdkafka RD_KAFKA_RESP_ERR_OFFSET_OUT_OF_RANGE (broker) and RD_KAFKA_RESP_ERR__AUTO_OFFSET_RESET
_OUT_OF_RANGE_CODES = frozenset({1, -140})
_OFFSET_BEGINNING = -2  # librdkafka RD_KAFKA_OFFSET_BEGINNING
_TRACEPARENT = re.compile(r"[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")
_STATUS_DETAIL_MAX = 300
_LOG_EVERY_S = 60.0
_MAX_BACKOFF_EXPONENT = 16
_REPLY, _DLQ = "reply", "dlq"


class _Executor(Protocol):
    def submit(self, fn: Callable[[], None], /) -> Any: ...

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None: ...


# ── pure helpers ────────────────────────────────────────────────────────────────────────────


def project_label(binding: StreamBinding) -> str:
    """The binding's project as a label — :func:`examlops.dataplane.streams.types.display_project`
    under the name this module already published (review M3)."""
    return display_project(binding.project)


def consumer_group(binding: StreamBinding) -> str:
    """``dataplane:{project}:{stream}`` — the project keeps two tenants out of one group."""
    return f"dataplane:{project_label(binding)}:{binding.name}"


def backoff_delay(attempt: int, rng: Callable[[], float] = random.random) -> float:
    """Full-jitter exponential backoff after failed attempt ``attempt`` (1-based)."""
    exponent = min(max(0, attempt - 1), _MAX_BACKOFF_EXPONENT)
    return max(0.0, rng()) * min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2**exponent))


def _clamp_retry_after(seconds: float) -> float:
    return min(RETRY_AFTER_MAX_S, max(0.0, float(seconds)))


#: How often (real seconds) the poll loop publishes ``dataplane_stream_consumer_lag``.
LAG_REPORT_INTERVAL_S = 1.0

#: ``IngressResult.shed_reason`` values that mark the ingress's own backpressure. Defined by the
#: ingress — the one that stamps them — and re-exported here under the name this module already
#: published (review M2): three copies of the two strings is one too many to keep in step.
#:
#: The signal is explicit: the ingress stamps it on its own sheds and on nothing else. Every other
#: ``overloaded`` — an upstream 429/503, or any result carrying no ``shed_reason`` at all — is a
#: failed attempt, so an unknown overload fails safe: bounded retries, then a dead letter.
LOCAL_SHED_REASONS = _LOCAL_SHED_REASONS
is_local_shed = _is_local_shed


class EnvelopeRejected(Exception):
    """A message that must be dead-lettered without inference: ``reason`` is one of the dlq
    ``REASON_*`` values, ``detail`` a payload-free explanation."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


#: The pre-R21 private name; A8's push route still imports it (the controller switches it later).
_Reject = EnvelopeRejected


def parse_value(
    raw: bytes | None, *, max_bytes: int
) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    """``(payload, model, alias, metadata)`` of one message value (see the module docstring).

    Raises :class:`EnvelopeRejected` for an oversize value (checked on the raw bytes, before parsing), a
    value that is not JSON, or one that is not a JSON object / not a well-formed envelope.
    ``model``/``alias`` are ``""`` when the message does not name them.
    """
    if raw is None:
        raise EnvelopeRejected(REASON_NOT_JSON, "message has no value")
    if len(raw) > max_bytes:
        raise EnvelopeRejected(
            REASON_OVERSIZE, f"message is {len(raw)} bytes; the limit is {max_bytes}"
        )
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        raise EnvelopeRejected(REASON_NOT_JSON, "message value is not JSON") from None
    if not isinstance(doc, dict):
        raise EnvelopeRejected(REASON_INVALID_MESSAGE, "message value must be a JSON object")
    inner = doc.get("payload")
    if not isinstance(inner, dict):
        return doc, "", "", {}
    model, alias, metadata = doc.get("model"), doc.get("alias"), doc.get("metadata")
    for name, value in (("model", model), ("alias", alias)):
        if value is not None and not isinstance(value, str):
            raise EnvelopeRejected(REASON_INVALID_MESSAGE, f"envelope {name} must be a string")
    if metadata is not None and not isinstance(metadata, dict):
        raise EnvelopeRejected(REASON_INVALID_MESSAGE, "envelope metadata must be an object")
    return inner, model or "", alias or "", dict(metadata or {})


def _header(headers: Any, name: str) -> Any:
    for item in headers or ():
        try:
            key, value = item
        except (TypeError, ValueError):
            continue
        if isinstance(key, str) and key.lower() == name:
            return value
    return None


def _header_text(value: Any) -> str | None:
    if isinstance(value, bytes | bytearray):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    return value if isinstance(value, str) else None


def read_traceparent(headers: Any) -> str | None:
    text = _header_text(_header(headers, "traceparent"))
    if text is None:
        return None
    text = text.strip()
    return text if _TRACEPARENT.fullmatch(text) else None


def read_idempotency_key(headers: Any) -> str | None:
    raw = _header(headers, "idempotency-key")
    if raw is None:
        return None
    text = _header_text(raw)
    if text is None or len(text.strip()) > IDEMPOTENCY_KEY_MAX:
        raise EnvelopeRejected(
            REASON_INVALID_MESSAGE,
            f"idempotency-key must be at most {IDEMPOTENCY_KEY_MAX} characters of UTF-8",
        )
    return text.strip() or None


def _reply_document(
    binding: StreamBinding,
    *,
    ok: bool,
    outcome: str,
    prediction: Any = None,
    version: Any = None,
    error: str | None = None,
) -> bytes:
    doc: dict[str, Any] = {
        "ok": ok,
        "outcome": outcome,
        "prediction": prediction,
        "model": binding.model,
        "version": None if version in (None, "") else str(version),
        "error": error,
    }
    try:
        return json.dumps(doc, allow_nan=False, default=str).encode("utf-8")
    except ValueError:  # a non-finite prediction: keep the answer, spelled as a string
        doc["prediction"] = str(prediction)
        return json.dumps(doc, default=str).encode("utf-8")


def encode_reply(
    binding: StreamBinding, result: InferenceResult, *, secrets: tuple[str, ...] = ()
) -> bytes:
    """The reply to an answered request: ``{"ok", "outcome", "prediction", "model", "version",
    "error"}`` — ``error`` redacted and bounded, ``None`` on ``ok``."""
    body = result.body if isinstance(result.body, dict) else {}
    error = None
    if result.outcome != "ok":
        error = redact_error(
            body.get("detail") or body.get("error") or result.outcome, secrets=secrets
        )
    return _reply_document(
        binding,
        ok=result.outcome == "ok",
        outcome=result.outcome,
        prediction=result.prediction,
        version=body.get("model_version"),
        error=error,
    )


# ── records, verdicts, partition state ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Record:
    """A snapshot of one consumed message; workers never touch the client's Message object."""

    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes | None
    headers: tuple[Any, ...]

    @property
    def origin(self) -> dict[str, Any]:
        return {"topic": self.topic, "partition": self.partition, "offset": self.offset}


@dataclass(frozen=True)
class _DeadLetter:
    """A dead letter owed for one offset, and which of its parts are already written."""

    reason: str
    error: str
    attempts: int
    reply_outcome: str
    sink_done: bool = False
    dlq_done: bool = False
    reply_done: bool = False
    gone: bool = False  # the message left the log: parts are (re)written from memory, value-less


@dataclass(frozen=True)
class _Verdict:
    """A worker's answer for one dispatch.

    ``answered``: terminal once every part in ``parts`` is delivered. ``retry``: back off
    (``counts`` — whether it spends an attempt). ``dead_letter``: the parts of ``dead_letter``
    written so far; ``failed`` when one could not be written (the sink raised, a produce raised).
    """

    action: str
    parts: tuple[str, ...] = ()
    reason: str = ""
    detail: str = ""
    retry_after: float | None = None
    counts: bool = True
    dead_letter: _DeadLetter | None = None
    failed: bool = False


@dataclass
class _Pending:
    token: int
    key: tuple[str, int]
    offset: int
    attempt: int
    handled: bool = False
    verdict: _Verdict | None = None
    delivered: set[str] = field(default_factory=set)
    delivery_errors: dict[str, str] = field(default_factory=dict)


@dataclass
class _PartitionState:
    """One partition's bookkeeping. ``outstanding`` is ``queued ∪ in_flight ∪ refetch``: every
    received offset not yet terminal. A received offset below ``high`` that is not outstanding is
    terminal."""

    topic: str
    partition: int
    outstanding: set[int] = field(default_factory=set)
    queued: set[int] = field(default_factory=set)  # in the local backlog
    in_flight: set[int] = field(default_factory=set)  # dispatched, not yet settled
    refetch: set[int] = field(default_factory=set)  # must be fetched again (see module docstring)
    attempts: dict[int, int] = field(default_factory=dict)  # attempts spent
    dl_tries: dict[int, int] = field(default_factory=dict)  # failed dead-letter writes
    dead_letter_next: dict[int, _DeadLetter] = field(default_factory=dict)
    #: key + traceparent header of each outstanding offset, to answer it if it leaves the log
    identity: dict[int, tuple[bytes | None, tuple[Any, ...]]] = field(default_factory=dict)
    #: gone from the log: the ``expired`` dead letter owed, and when to (re)write it
    expired: dict[int, tuple[_DeadLetter, float]] = field(default_factory=dict)
    seek_frontier: int | None = None  # a seek here has not yet returned its first message
    high: int | None = None  # one past the highest offset received
    fetch_pos: int | None = None  # where the consumer fetches next
    stored: int | None = None  # the store position: the first offset seen, then each stored one
    has_stored: bool = False  # store_offsets() has actually been called for this partition
    retry_until: float | None = None
    retry_reason: str | None = None
    paused: bool = False  # the consumer's actual pause state
    dirty: bool = False

    def seek_target(self) -> int | None:
        """The lowest offset awaiting re-fetch — the only place a seek may go."""
        return min(self.refetch) if self.refetch else None

    def position(self) -> int | None:
        """``min(outstanding ∪ {high, seek target})``: never past a non-terminal offset."""
        candidates = set(self.outstanding)
        if self.high is not None:
            candidates.add(self.high)
        target = self.seek_target()
        if target is not None:
            candidates.add(target)
        return min(candidates) if candidates else None

    def is_terminal(self, offset: int) -> bool:
        """A received offset that reached a terminal result (redelivery is skipped)."""
        return (self.stored is not None and offset < self.stored) or (
            self.high is not None and offset < self.high and offset not in self.outstanding
        )

    def terminal(self, offset: int) -> None:
        self.outstanding.discard(offset)
        self.queued.discard(offset)
        self.in_flight.discard(offset)
        self.refetch.discard(offset)
        self.attempts.pop(offset, None)
        self.dl_tries.pop(offset, None)
        self.dead_letter_next.pop(offset, None)
        self.identity.pop(offset, None)
        self.expired.pop(offset, None)
        self.dirty = True


# ── the session ─────────────────────────────────────────────────────────────────────────────


class KafkaStreamSession:
    """One running Kafka stream: the poll loop and its partition bookkeeping.

    Build it with :meth:`KafkaStreamConnector.open_session`; drive it with :meth:`run` (or
    :meth:`start` + :meth:`step` + :meth:`shutdown`, which is what :meth:`run` does).
    """

    def __init__(
        self,
        binding: StreamBinding,
        ingress: StreamIngress,
        *,
        consumer_factory: Callable[[dict[str, Any]], Any],
        consumer_conf: dict[str, Any],
        producer_factory: Callable[[dict[str, Any]], Any] | None,
        producer_conf: dict[str, Any] | None,
        topic: str,
        reply_topic: str | None,
        dlq_topic: str | None,
        dlq: DeadLetterSink,
        status_cb: StatusCallback,
        executor_factory: Callable[[int], _Executor],
        clock: Callable[[], float],
        rng: Callable[[], float],
        poll_timeout_s: float,
        drain_timeout_s: float,
        secrets: tuple[str, ...] = (),
        paused: bool = False,
        start: str = "earliest",
    ) -> None:
        self.binding = binding
        self._ingress = ingress
        self._start = start
        self._told_start_at_end = False
        self._told_committed_unknown = False
        self._topic = topic
        self._reply_topic = reply_topic
        self._dlq_topic = dlq_topic
        self._dlq = dlq
        self._status_cb = status_cb
        self._clock = clock
        self._rng = rng
        self._poll_timeout = poll_timeout_s
        self._drain_timeout = drain_timeout_s
        self._secrets = secrets
        self._label = f"{project_label(binding)}/{binding.name}"
        self.capacity = max(1, min(int(binding.limits.max_in_flight), MAX_WORKERS))
        self._max_attempts = max(1, int(binding.limits.max_attempts))
        self._backlog_max = self.capacity
        self._pending_max = self.capacity * 4

        conf = dict(consumer_conf)
        conf["error_cb"] = self._on_client_error
        self._consumer = consumer_factory(conf)
        self._producer: Any = None
        try:
            if producer_factory is not None and producer_conf is not None:
                pconf = dict(producer_conf)
                pconf["error_cb"] = self._on_producer_error
                self._producer = producer_factory(pconf)
            self._executor = executor_factory(self.capacity)
        except BaseException:
            try:
                self._consumer.close()  # never leak a group member we will not drive
            except Exception:  # noqa: BLE001
                logger.debug("closing a half-built kafka consumer failed", exc_info=True)
            raise

        self._events: SimpleQueue[tuple[Any, ...]] = SimpleQueue()
        self._parts: dict[tuple[str, int], _PartitionState] = {}
        self._backlog: deque[_Record] = deque()
        self._pending: dict[int, _Pending] = {}
        self._tokens = itertools.count(1)
        self._busy = 0
        self._flow_paused = False
        self._stopping = False
        self._fatal: str | None = None
        self._transient_error: str | None = None
        self._last_status: tuple[str, str | None] | None = None
        self._last_lag_report = float("-inf")
        self._last_warning: dict[str, float] = {}
        self._ctl_lock = threading.Lock()
        self._operator_paused = paused
        self._op = paused  # the loop's snapshot of _operator_paused
        self._closed = False

    # ── control (any thread) ─────────────────────────────────────────────────────────────────

    def set_paused(self, paused: bool) -> None:
        """Pause (or resume) the whole assignment. Thread-safe; applied by the loop's next
        iteration, which keeps polling — group membership and heartbeats are unaffected."""
        with self._ctl_lock:
            self._operator_paused = bool(paused)

    @property
    def paused(self) -> bool:
        with self._ctl_lock:
            return self._operator_paused

    # ── lifecycle (the loop thread) ──────────────────────────────────────────────────────────

    def run(self, stop_event: threading.Event) -> None:
        """Poll until ``stop_event`` is set, then drain and commit. Raises on a fatal error,
        after committing what is already safe and closing the consumer."""
        try:
            self.start()
            while not stop_event.is_set():
                self.step()
            self.shutdown()
        except BaseException as exc:
            self._report(
                "error", redact_error(f"{type(exc).__name__}: {exc}", secrets=self._secrets)
            )
            self._abort()
            raise

    def start(self) -> None:
        self._consumer.subscribe(
            [self._topic],
            on_assign=self._on_assign,
            on_revoke=self._on_revoke,
            on_lost=self._on_lost,
        )
        self._report("running", None)

    def step(self) -> None:
        """One loop iteration: settle results, dispatch, (un)pause, store, then ``poll()``."""
        now = self._clock()
        self._apply_control()
        self._serve_producer()
        self._drain_events()
        self._update_flow()
        self._dispatch()
        self._reconcile(now)
        self._store_ready()
        self._report_status(now)
        msg = self._consumer.poll(self._poll_timeout)
        if msg is not None:
            self._on_message(msg, self._clock())
            self._update_flow()
            self._dispatch()
        if self._fatal is not None:
            raise DataplaneError(self._fatal)

    def shutdown(self) -> None:
        """Stop dispatching, let in-flight work and deliveries finish (bounded by the drain
        timeout, polling throughout), store, commit, close."""
        self._stopping = True
        self._purge_backlog(None)
        deadline = self._clock() + self._drain_timeout
        while (self._busy > 0 or self._pending) and self._clock() < deadline:
            self._serve_producer()
            self._drain_events()
            now = self._clock()
            self._reconcile(now)
            self._store_ready()
            msg = self._consumer.poll(min(self._poll_timeout, max(0.0, deadline - now)))
            if msg is not None:
                self._on_message(msg, self._clock())  # parked: every partition is paused
        if self._producer is not None:
            try:
                self._producer.flush(max(0.0, deadline - self._clock()))
            except Exception as exc:  # noqa: BLE001 - shutdown commits whatever it can
                self._warn("flush", f"producer flush failed ({type(exc).__name__})")
        self._drain_events()
        self._store_ready()
        self._commit(list(self._parts.values()), "shutdown")
        self._close()
        self._report("stopped", None)

    def _abort(self) -> None:
        """A fatal error: commit what is already safe, then close. Never raises."""
        try:
            self._drain_events()
            self._store_ready()
            self._commit(list(self._parts.values()), "abort")
        except Exception:  # noqa: BLE001 - best effort on the way out
            logger.debug("dataplane stream %s: commit on abort failed", self._label, exc_info=True)
        self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._consumer.close()
        except Exception as exc:  # noqa: BLE001 - closing must not raise
            self._warn("close", f"consumer close failed ({type(exc).__name__})")
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            logger.debug("dataplane stream %s: executor shutdown failed", self._label)

    # ── introspection (tests, status) ────────────────────────────────────────────────────────

    def stored_offsets(self) -> dict[int, int | None]:
        return {st.partition: st.stored for st in self._parts.values()}

    def retrying_partitions(self) -> list[int]:
        now = self._clock()
        return sorted(
            st.partition
            for st in self._parts.values()
            if st.retry_until is not None and now < st.retry_until
        )

    def partition_state(self, partition: int) -> _PartitionState | None:
        return self._parts.get((self._topic, partition))

    @property
    def busy(self) -> int:
        return self._busy

    # ── rebalance callbacks (called from inside poll(), on the loop thread) ─────────────────

    def _on_assign(self, consumer: Any, partitions: list[Any]) -> None:
        self._last_lag_report = float("-inf")  # a new assignment reports its lag at once
        for tp in partitions:
            key = (str(tp.topic), int(tp.partition))
            self._parts[key] = _PartitionState(key[0], key[1])
        self._transient_error = None
        if self._start == "latest":
            self._start_uncommitted_at_end(consumer, partitions)

    def _start_uncommitted_at_end(self, consumer: Any, partitions: list[Any]) -> None:
        """``start: latest``: a partition the group has no committed offset for starts at the
        high watermark — set in the assignment itself, so before its first fetch — and that
        start is stored, so the group commits it and a later assignment resumes there. A
        committed partition keeps its committed offset. Any lookup failing falls back to
        ``earliest`` (a replay, never a skip)."""
        try:
            committed = consumer.committed(list(partitions), timeout=5.0)
        except Exception as exc:  # noqa: BLE001 - replaying beats skipping
            self._warn("start", f"committed offsets unreadable ({type(exc).__name__}); earliest")
            return
        uncommitted: set[tuple[str, int]] = set()
        unknown: list[int] = []
        for c in committed or ():
            if getattr(c, "error", None) is not None or c.offset is None:
                unknown.append(int(c.partition))  # unknown is not "none": replay, never skip
            elif int(c.offset) < 0:
                uncommitted.add((str(c.topic), int(c.partition)))
        if unknown and not self._told_committed_unknown:
            self._told_committed_unknown = True
            logger.warning(
                "dataplane stream %s: committed offset unknown for partition(s) %s; they start "
                "as start=earliest would (a replay, never a skip)",
                self._label,
                ", ".join(str(p) for p in sorted(unknown)),
            )
        ends: dict[tuple[str, int], int] = {}
        for topic, partition in sorted(uncommitted):
            try:
                tp = _kafka._tp(topic, partition)
                ends[(topic, partition)] = int(consumer.get_watermark_offsets(tp, timeout=5.0)[1])
            except Exception as exc:  # noqa: BLE001 - this partition starts at earliest
                self._warn("start", f"log end unreadable ({type(exc).__name__}); earliest")
        if not ends:
            return
        assignment = [
            _kafka._tp(t, p, ends.get((t, p), _OFFSET_INVALID)) for t, p in self._keys(partitions)
        ]
        try:
            consumer.assign(assignment)
        except Exception as exc:  # noqa: BLE001 - the default assignment applies: earliest
            self._warn("start", f"could not position at the log end ({type(exc).__name__})")
            return
        for key, end in ends.items():
            st = self._parts.get(key)
            if st is not None:  # store (so the group commits) the start: a later rebalance
                st.high = end  # resumes here instead of jumping to a newer end
                st.fetch_pos = end
                st.dirty = True
        where = ", ".join(f"{p}@{end}" for (_t, p), end in sorted(ends.items()))
        if not self._told_start_at_end:
            self._told_start_at_end = True
            logger.info(
                "dataplane stream %s: start=latest and no committed offset, so it starts at the "
                "log end (partition@offset: %s); earlier messages are not replayed",
                self._label,
                where,
            )
        else:
            logger.debug(
                "dataplane stream %s: new partitions start at the log end: %s", self._label, where
            )

    def _on_revoke(self, consumer: Any, partitions: list[Any]) -> None:
        """Settle what has finished, store it and commit it synchronously *before* the
        partitions go; work still in flight for them is left to their next owner."""
        self._serve_producer()
        self._drain_events()
        self._store_ready()
        states = [self._parts[k] for k in self._keys(partitions) if k in self._parts]
        self._commit(states, "revoke")
        self._forget(partitions)

    def _on_lost(self, consumer: Any, partitions: list[Any]) -> None:
        """Partitions lost without a clean revoke: they may already be someone else's, so
        nothing is stored or committed for them."""
        self._forget(partitions)

    @staticmethod
    def _keys(partitions: list[Any]) -> list[tuple[str, int]]:
        return [(str(tp.topic), int(tp.partition)) for tp in partitions]

    def _forget(self, partitions: list[Any]) -> None:
        keys = set(self._keys(partitions))
        for key in keys:
            self._parts.pop(key, None)
            # A partition we no longer own must stop reporting a lag: its last value would stand
            # for ever and alert on a backlog whichever replica took it over is already serving.
            metrics.clear_consumer_lag(self.binding.project, self.binding.name, key[1])
        self._backlog = deque(r for r in self._backlog if (r.topic, r.partition) not in keys)
        for token in [t for t, p in self._pending.items() if p.key in keys]:
            del self._pending[token]  # their late results are ignored

    # ── messages ─────────────────────────────────────────────────────────────────────────────

    def _on_message(self, msg: Any, now: float) -> None:
        err = msg.error()
        if err is not None:
            self._on_client_error(err, _message_key(msg))
            return
        rec = _Record(
            topic=str(msg.topic()),
            partition=int(msg.partition()),
            offset=int(msg.offset()),
            key=msg.key(),
            value=msg.value(),
            headers=tuple(msg.headers() or ()),
        )
        st = self._parts.get((rec.topic, rec.partition))
        if st is None:
            return  # not (or no longer) ours
        self._transient_error = None
        o = rec.offset
        st.fetch_pos = o + 1
        if st.stored is None:
            st.stored = o  # the first offset seen is where the group already stands
        if st.seek_frontier is not None and o >= st.seek_frontier:
            # the first message after a seek to `frontier`: parked offsets below it are gone
            frontier, st.seek_frontier = st.seek_frontier, None
            gone = sorted(r for r in st.refetch if frontier <= r < o)
            if gone:
                self._expire(st, gone)
        if st.is_terminal(o) or o in st.queued or o in st.in_flight or o in st.expired:
            return  # terminal, already being handled, or being dead-lettered as expired
        st.high = o + 1 if st.high is None else max(st.high, o + 1)
        st.outstanding.add(o)
        st.identity[o] = (
            rec.key,
            tuple(h for h in rec.headers if _is_header(h, "traceparent")),
        )
        if self._desired_paused(st, now) or len(self._backlog) >= self._backlog_max:
            # arrived from a paused partition (or the backlog is full): fetch it again later
            st.refetch.add(o)
            if len(self._backlog) >= self._backlog_max:
                self._flow_paused = True
            return
        st.refetch.discard(o)
        st.queued.add(o)
        self._backlog.append(rec)

    def _desired_paused(self, st: _PartitionState, now: float) -> bool:
        return (
            self._stopping
            or self._op
            or self._flow_paused
            or (st.retry_until is not None and now < st.retry_until)
        )

    def _apply_control(self) -> None:
        with self._ctl_lock:
            op = self._operator_paused
        if op and not self._op:
            self._purge_backlog(None)
        self._op = op

    def _update_flow(self) -> None:
        if len(self._backlog) >= self._backlog_max or len(self._pending) >= self._pending_max:
            self._flow_paused = True
        elif (
            self._flow_paused
            and len(self._backlog) <= self._backlog_max // 2
            and len(self._pending) <= self._pending_max // 2
        ):
            self._flow_paused = False

    def _purge_backlog(self, only: _PartitionState | None) -> None:
        """Take not-yet-dispatched messages (of one partition, or all) out of the backlog; they
        stay outstanding, awaiting re-fetch."""
        kept: deque[_Record] = deque()
        for rec in self._backlog:
            st = self._parts.get((rec.topic, rec.partition))
            if st is None:
                continue
            if only is not None and st is not only:
                kept.append(rec)
                continue
            st.queued.discard(rec.offset)
            st.refetch.add(rec.offset)
        self._backlog = kept

    def _dispatch(self) -> None:
        while (
            self._backlog
            and not self._stopping
            and self._busy < self.capacity
            and len(self._pending) < self._pending_max
        ):
            rec = self._backlog.popleft()
            key = (rec.topic, rec.partition)
            st = self._parts.get(key)
            o = rec.offset
            if st is None or o not in st.queued:
                continue
            st.queued.discard(o)
            st.in_flight.add(o)
            token = next(self._tokens)
            owed = st.dead_letter_next.get(o)
            if owed is not None:
                attempt = st.attempts.get(o, 0)
                job = self._job(token, rec, attempt, owed)
            else:
                attempt = st.attempts.get(o, 0) + 1
                job = self._job(token, rec, attempt, None)
            self._pending[token] = _Pending(token, key, o, attempt)
            self._busy += 1
            try:
                self._executor.submit(job)
            except RuntimeError:  # the pool is shutting down: undo; the offset is re-fetched
                self._busy -= 1
                del self._pending[token]
                st.in_flight.discard(o)
                st.refetch.add(o)
        self._dispatch_expired()

    def _dispatch_expired(self) -> None:
        """Write the ``expired`` dead letters that are due. Their messages are gone from the log,
        so the job gets a payload-free record rebuilt from the kept key and traceparent."""
        now = self._clock()
        for st in list(self._parts.values()):
            for offset in sorted(st.expired):
                if (
                    self._stopping
                    or self._busy >= self.capacity
                    or len(self._pending) >= self._pending_max
                ):
                    return
                owed, due = st.expired[offset]
                if due > now:
                    continue
                del st.expired[offset]
                key, headers = st.identity.get(offset, (None, ()))
                rec = _Record(st.topic, st.partition, offset, key, None, headers)
                token = next(self._tokens)
                self._pending[token] = _Pending(
                    token, (st.topic, st.partition), offset, owed.attempts
                )
                st.in_flight.add(offset)
                self._busy += 1
                try:
                    self._executor.submit(self._job(token, rec, owed.attempts, owed))
                except RuntimeError:  # shutting down: keep it owed
                    self._busy -= 1
                    del self._pending[token]
                    st.in_flight.discard(offset)
                    st.expired[offset] = (owed, due)
                    return

    def _expire(self, st: _PartitionState, gone: list[int]) -> None:
        """Parked offsets the log no longer holds: dead-letter each as ``expired_from_log`` (no
        payload), answer it, count it — they stay outstanding until that is written.

        An offset already owing a dead letter (its sink record written with the payload, say,
        and its dlq copy or reply still failing) keeps that dead letter — reason, error and the
        parts already written: only the missing parts are finished, value-less, so the sink never
        gets a second record for the origin and the requester never a second, contradicting
        reply. Only an offset with no dead letter of its own is ``expired_from_log``."""
        now = self._clock()
        for offset in gone:
            st.refetch.discard(offset)
            prior = st.dead_letter_next.pop(offset, None)
            if prior is not None:
                owed = replace(prior, gone=True)
            else:
                error = (
                    f"offset {offset} left the log before it could be re-fetched "
                    "(retention, compaction or DeleteRecords)"
                )
                owed = _DeadLetter(
                    REASON_EXPIRED, error, st.attempts.get(offset, 0), EXPIRED_OUTCOME, gone=True
                )
            st.expired[offset] = (owed, now)
        metrics.messages_expired(self.binding.project, self.binding.name, len(gone))
        logger.warning(
            "dataplane stream %s: %d parked message(s) left the log before they could be "
            "re-fetched (partition %d, offsets %d..%d); dead-lettering them (as %s unless a "
            "dead letter was already owed)",
            self._label,
            len(gone),
            st.partition,
            gone[0],
            gone[-1],
            REASON_EXPIRED,
        )

    def _out_of_range(self, key: tuple[str, int] | None) -> None:
        """librdkafka reports the fetch position out of range. Parked offsets below the
        partition's low watermark are gone (expired now); the fetch restarts at the log start,
        and for parked offsets at or above it the gone-offset check decides on the first
        message there."""
        st = self._parts.get(key) if key is not None else None
        if st is None:
            return
        tp = _kafka._tp(st.topic, st.partition, _OFFSET_BEGINNING)
        low: int | None = None
        if st.refetch:
            try:
                low = int(self._consumer.get_watermark_offsets(tp, timeout=1.0)[0])
            except Exception:  # noqa: BLE001 - unknown: the frontier check below still decides
                low = None
            below = sorted(r for r in st.refetch if low is not None and r < low)
            if below:
                self._expire(st, below)
        try:
            self._consumer.seek(tp)
        except Exception as exc:  # noqa: BLE001 - librdkafka's own reset still applies
            self._warn("seek", f"seek to the log start failed ({type(exc).__name__})")
            return
        target = st.seek_target()
        if target is not None:
            st.seek_frontier = target if st.seek_frontier is None else min(st.seek_frontier, target)
        # sought to the log start: never seek back to a parked offset below it
        st.fetch_pos = max(p for p in (low, target, 0) if p is not None)

    # ── (un)pause, seek, store, commit ───────────────────────────────────────────────────────

    def _reconcile(self, now: float) -> None:
        to_pause: list[_PartitionState] = []
        to_resume: list[_PartitionState] = []
        for st in list(self._parts.values()):
            if self._desired_paused(st, now):
                if not st.paused:
                    to_pause.append(st)
                continue
            if st.retry_until is not None:
                st.retry_until = None
                st.retry_reason = None
            target = st.seek_target()
            if target is not None and (st.fetch_pos is None or target < st.fetch_pos):
                try:
                    self._consumer.seek(_kafka._tp(st.topic, st.partition, target))
                except Exception as exc:  # noqa: BLE001 - stay paused; try again next iteration
                    self._warn("seek", f"seek failed ({type(exc).__name__}); retrying")
                    if not st.paused:
                        to_pause.append(st)
                    continue
                st.fetch_pos = target
                st.seek_frontier = target
            if st.paused:
                to_resume.append(st)
        if to_pause:
            try:
                self._consumer.pause([_kafka._tp(s.topic, s.partition) for s in to_pause])
                for s in to_pause:
                    s.paused = True
            except Exception as exc:  # noqa: BLE001 - late arrivals are parked and re-fetched
                self._warn("pause", f"pause failed ({type(exc).__name__})")
        if to_resume:
            try:
                self._consumer.resume([_kafka._tp(s.topic, s.partition) for s in to_resume])
                for s in to_resume:
                    s.paused = False
            except Exception as exc:  # noqa: BLE001 - retried next iteration
                self._warn("resume", f"resume failed ({type(exc).__name__})")

    def _store_ready(self) -> None:
        ready: list[tuple[_PartitionState, int]] = []
        for st in self._parts.values():
            if not st.dirty:
                continue
            st.dirty = False
            pos = st.position()
            if pos is not None and (st.stored is None or pos > st.stored):
                ready.append((st, pos))
        if not ready:
            return
        try:
            self._consumer.store_offsets(
                offsets=[_kafka._tp(st.topic, st.partition, pos) for st, pos in ready]
            )
        except Exception as exc:  # noqa: BLE001 - kept dirty, stored on a later iteration
            self._warn("store", f"store_offsets failed ({type(exc).__name__})")
            for st, _pos in ready:
                st.dirty = True
            return
        for st, pos in ready:
            st.stored = pos
            st.has_stored = True

    def _commit(self, states: list[_PartitionState], why: str) -> None:
        offsets = [
            _kafka._tp(st.topic, st.partition, st.stored)
            for st in states
            if st.has_stored and st.stored is not None
        ]
        if not offsets:
            return
        try:
            self._consumer.commit(offsets=offsets, asynchronous=False)
        except Exception as exc:  # noqa: BLE001 - auto-commit still carries the stored offsets
            self._warn(f"commit-{why}", f"commit on {why} failed ({type(exc).__name__})")

    # ── worker results and delivery reports ──────────────────────────────────────────────────

    def _serve_producer(self) -> None:
        if self._producer is None:
            return
        try:
            self._producer.poll(0)
        except Exception as exc:  # noqa: BLE001
            self._warn("producer-poll", f"producer poll failed ({type(exc).__name__})")

    def _drain_events(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except Empty:
                return
            if event[0] == "handled":
                _, token, verdict = event
                self._busy -= 1
                pending = self._pending.get(token)
                if pending is not None:
                    pending.handled = True
                    pending.verdict = verdict
                    self._resolve(pending)
            else:  # "delivery"
                _, token, part, ok, error = event
                pending = self._pending.get(token)
                if pending is not None:
                    if ok:
                        pending.delivered.add(part)
                    else:
                        pending.delivery_errors.setdefault(part, error or "delivery failed")
                    self._resolve(pending)

    def _resolve(self, pending: _Pending) -> None:
        verdict = pending.verdict
        if not pending.handled or verdict is None:
            return
        reported = pending.delivered | set(pending.delivery_errors)
        if any(part not in reported for part in verdict.parts):
            return  # a delivery report is still to come
        self._pending.pop(pending.token, None)
        st = self._parts.get(pending.key)
        if st is None:
            return  # revoked meanwhile
        failure = next(iter(pending.delivery_errors.values()), "")
        if verdict.action == "answered":
            if pending.delivery_errors:
                self._retry(st, pending, "reply_delivery", None, failure, counts=True)
            else:
                st.terminal(pending.offset)
        elif verdict.action == "retry":
            self._retry(
                st, pending, verdict.reason, verdict.retry_after, verdict.detail, verdict.counts
            )
        else:
            owed = verdict.dead_letter
            if owed is None:  # cannot happen: every dead_letter verdict carries its owed parts
                self._retry(st, pending, "internal", None, "dead letter lost", counts=True)
                return
            if _DLQ in pending.delivered:
                owed = replace(owed, dlq_done=True)
            if _REPLY in pending.delivered:
                owed = replace(owed, reply_done=True)
            if verdict.failed or pending.delivery_errors:
                self._dead_letter_retry(st, pending.offset, owed, verdict.detail or failure)
            else:
                st.terminal(pending.offset)

    def _retry(
        self,
        st: _PartitionState,
        pending: _Pending,
        reason: str,
        retry_after: float | None,
        detail: str,
        counts: bool,
    ) -> None:
        o, attempt = pending.offset, pending.attempt
        if not counts:  # backpressure the stream put on itself: wait, spend nothing
            delay = _clamp_retry_after(
                LOCAL_SHED_RETRY_AFTER_S if retry_after is None else retry_after
            )
        else:
            st.attempts[o] = attempt
            if attempt >= self._max_attempts:
                error = redact_error(
                    f"{reason} after {attempt} attempts" + (f": {detail}" if detail else ""),
                    secrets=self._secrets,
                )
                st.dead_letter_next[o] = _DeadLetter(
                    REASON_RETRIES_EXHAUSTED, error, attempt, EXHAUSTED_OUTCOME
                )
                delay = 0.0
            elif retry_after is not None:
                delay = _clamp_retry_after(retry_after)
            else:
                delay = backoff_delay(attempt, self._rng)
        self._to_refetch(st, o, delay, reason)

    def _dead_letter_retry(
        self, st: _PartitionState, offset: int, owed: _DeadLetter, detail: str
    ) -> None:
        """A dead letter could not be fully written: redo only its missing parts, with backoff,
        for as long as it takes — the offset is not stored meanwhile."""
        tries = st.dl_tries.get(offset, 0) + 1
        st.dl_tries[offset] = tries
        self._warn(
            "dlq-write",
            f"dead letter for partition {st.partition} offset {offset} not written "
            f"({redact_error(detail, secrets=self._secrets, max_bytes=_STATUS_DETAIL_MAX)}); "
            "retrying",
        )
        delay = backoff_delay(tries, self._rng)
        if owed.gone:  # nothing to re-fetch: rewrite the missing parts from memory
            st.in_flight.discard(offset)
            st.expired[offset] = (owed, self._clock() + delay)
            return
        st.dead_letter_next[offset] = owed
        self._to_refetch(st, offset, delay, "dlq_write")

    def _to_refetch(self, st: _PartitionState, offset: int, delay: float, reason: str) -> None:
        """Park ``offset`` for re-fetch and pause ``st`` for ``delay`` s; the partition's queued
        messages are parked with it. It resumes with a seek back to its lowest parked offset."""
        st.in_flight.discard(offset)
        st.queued.discard(offset)
        st.refetch.add(offset)
        until = self._clock() + delay
        st.retry_until = until if st.retry_until is None else max(st.retry_until, until)
        st.retry_reason = reason
        self._purge_backlog(st)

    # ── the worker side (pool threads) ───────────────────────────────────────────────────────

    def _job(
        self, token: int, rec: _Record, attempt: int, owed: _DeadLetter | None
    ) -> Callable[[], None]:
        def job() -> None:
            verdict = _Verdict("retry", reason="internal")
            try:
                if owed is not None:
                    verdict = self._dead_letter(token, rec, owed)
                else:
                    verdict = self._process(token, rec, attempt)
            except Exception as exc:  # noqa: BLE001 - a worker never dies; the message retries
                logger.warning(
                    "dataplane stream %s: worker failed on partition %d offset %d (%s)",
                    self._label,
                    rec.partition,
                    rec.offset,
                    type(exc).__name__,
                )
                if owed is not None:
                    verdict = _Verdict(
                        "dead_letter", dead_letter=owed, failed=True, detail=type(exc).__name__
                    )
                else:
                    verdict = _Verdict("retry", reason="internal", detail=type(exc).__name__)
            finally:
                self._events.put(("handled", token, verdict))

        return job

    def _process(self, token: int, rec: _Record, attempt: int) -> _Verdict:
        try:
            req = self._request_of(rec)
        except EnvelopeRejected as reject:
            error = redact_error(f"{reject.reason}: {reject.detail}", secrets=self._secrets)
            owed = _DeadLetter(reject.reason, error, attempt, REJECTED_OUTCOME)
            return self._dead_letter(token, rec, owed)
        parts: list[str] = []

        def reply(result: IngressResult) -> None:
            if self._reply_topic is None or result.outcome not in ANSWERED:
                return  # failures are answered by the dead letter, after it is written
            headers = [("traceparent", req.traceparent.encode())] if req.traceparent else None
            self._produce(
                self._reply_topic,
                encode_reply(self.binding, result, secrets=self._secrets),
                rec.key,
                headers,
                token,
                _REPLY,
            )
            parts.append(_REPLY)

        try:
            result = self._ingress.handle(self.binding, req, reply=reply)
        except Exception as exc:  # noqa: BLE001 - only reply() raises out of handle()
            detail = redact_error(f"{type(exc).__name__}: {exc}", secrets=self._secrets)
            return _Verdict("retry", reason="reply_produce", detail=detail)
        outcome = result.outcome
        if outcome in ANSWERED:
            return _Verdict("answered", parts=tuple(parts))
        detail = _result_detail(result)
        if outcome in RETRYABLE:
            if is_local_shed(result):
                return _Verdict(
                    "retry", reason="shed", retry_after=result.retry_after, counts=False
                )
            return _Verdict("retry", reason=outcome, detail=detail, retry_after=result.retry_after)
        error = redact_error(detail or outcome, secrets=self._secrets)
        return self._dead_letter(token, rec, _DeadLetter(outcome, error, attempt, outcome))

    def _request_of(self, rec: _Record) -> StreamRequest:
        limit = int(self.binding.limits.max_bytes)
        if rec.value is not None and len(rec.value) > limit:  # before headers, before any parse
            raise EnvelopeRejected(
                REASON_OVERSIZE, f"message is {len(rec.value)} bytes; the limit is {limit}"
            )
        idempotency_key = read_idempotency_key(rec.headers)
        payload, model, alias, metadata = parse_value(rec.value, max_bytes=limit)
        metadata.pop("tenant", None)  # defence in depth; the ingress never reads it (I4)
        if rec.key is not None:
            raw_key = rec.key
            key_text = (
                bytes(raw_key).decode("utf-8", "replace")
                if isinstance(raw_key, bytes | bytearray)
                else str(raw_key)
            )
            metadata["key"] = key_text
            metadata.setdefault("job_id", key_text)
        return StreamRequest(
            stream=self.binding.name,
            model=model,
            alias=alias,
            payload=payload,
            metadata=metadata,
            idempotency_key=idempotency_key,
            traceparent=read_traceparent(rec.headers),
        )

    def _dead_letter(self, token: int, rec: _Record, owed: _DeadLetter) -> _Verdict:
        """Write the parts of ``owed`` not yet written: the sink record first (nothing else is
        sent until it succeeds), then the ``dlq_topic`` copy and the failure reply."""
        if not owed.sink_done:
            try:
                self._dlq.record(
                    self.binding,
                    reason=owed.reason,
                    error=owed.error,
                    attempts=owed.attempts,
                    payload=rec.value,
                    origin=rec.origin,
                )
            except Exception as exc:  # noqa: BLE001 - never swallowed: the write is retried
                return _Verdict(
                    "dead_letter",
                    dead_letter=owed,
                    failed=True,
                    detail=f"dead-letter sink failed ({type(exc).__name__})",
                )
            owed = replace(owed, sink_done=True)
        parts: list[str] = []
        try:
            if self._dlq_topic is not None and not owed.dlq_done:
                headers = [
                    ("x-examlops-error", owed.error.encode("utf-8")),
                    ("x-examlops-stream", self._label.encode("utf-8")),
                    ("x-examlops-attempts", str(owed.attempts).encode("ascii")),
                    ("x-examlops-origin", json.dumps(rec.origin, sort_keys=True).encode("utf-8")),
                ]
                self._produce(self._dlq_topic, rec.value, rec.key, headers, token, _DLQ)
                parts.append(_DLQ)
            if self._reply_topic is not None and not owed.reply_done:
                traceparent = read_traceparent(rec.headers)
                self._produce(
                    self._reply_topic,
                    _reply_document(
                        self.binding, ok=False, outcome=owed.reply_outcome, error=owed.error
                    ),
                    rec.key,
                    [("traceparent", traceparent.encode())] if traceparent else None,
                    token,
                    _REPLY,
                )
                parts.append(_REPLY)
        except Exception as exc:  # noqa: BLE001 - retried; parts already queued are awaited
            detail = redact_error(f"{type(exc).__name__}: {exc}", secrets=self._secrets)
            return _Verdict(
                "dead_letter", parts=tuple(parts), dead_letter=owed, failed=True, detail=detail
            )
        return _Verdict("dead_letter", parts=tuple(parts), dead_letter=owed)

    def _produce(
        self,
        topic: str,
        value: bytes | None,
        key: bytes | None,
        headers: list[tuple[str, bytes]] | None,
        token: int,
        part: str,
    ) -> None:
        events = self._events
        secrets = self._secrets

        def on_delivery(err: Any, _msg: Any) -> None:
            if err is None:
                events.put(("delivery", token, part, True, None))
            else:
                text = redact_error(_error_text(err), secrets=secrets)
                events.put(("delivery", token, part, False, text))

        for attempt in (1, 2):
            try:
                self._producer.produce(
                    topic, value=value, key=key, headers=headers, on_delivery=on_delivery
                )
                return
            except BufferError:  # the local queue is full: serve reports once, then retry
                if attempt == 2:
                    raise
                self._producer.poll(0.5)

    # ── errors and status ────────────────────────────────────────────────────────────────────

    def _on_client_error(self, err: Any, key: tuple[str, int] | None = None) -> None:
        code = _call(err, "code")
        if code == _PARTITION_EOF:
            return
        if code in _OUT_OF_RANGE_CODES:
            self._warn("out-of-range", f"fetch position out of range on {key} (code {code})")
            self._out_of_range(key)
            return
        text = redact_error(
            f"kafka: {_error_text(err)}", secrets=self._secrets, max_bytes=_STATUS_DETAIL_MAX
        )
        if _call(err, "fatal") is True:
            self._fatal = text
            return
        self._transient_error = text
        self._warn("consumer", text)

    def _on_producer_error(self, err: Any) -> None:
        text = redact_error(
            f"kafka producer: {_error_text(err)}",
            secrets=self._secrets,
            max_bytes=_STATUS_DETAIL_MAX,
        )
        if _call(err, "fatal") is True:
            # an idempotent producer's fatal error: nothing it sends will be delivered again
            self._fatal = text
            return
        self._warn("producer", text)

    def _report_lag(self) -> None:
        """Publish ``dataplane_stream_consumer_lag`` per owned partition (ADR 0131 d11, M14).

        The lag is the partition's high watermark minus this consumer's own position — the offset
        it would resume from, i.e. ``_PartitionState.position()`` (never past a non-terminal
        offset), falling back to the stored offset. The watermark is read with ``cached=True``:
        librdkafka keeps the last value every fetch response carried, so this costs no broker
        round trip and is safe on the poll loop. A client that cannot answer from cache (or has
        not fetched yet) simply publishes nothing this pass.
        """
        for (topic, partition), st in list(self._parts.items()):
            try:
                tp = _kafka._tp(topic, partition)
                high = int(self._consumer.get_watermark_offsets(tp, cached=True)[1])
            except Exception:  # noqa: BLE001 - no cached watermark yet, or an old client
                continue
            if high < 0:  # librdkafka's "unknown" sentinel
                continue
            position = st.position()
            if position is None:
                position = st.stored
            if position is None or position < 0:
                continue
            metrics.set_consumer_lag(
                self.binding.project, self.binding.name, partition, high - position
            )

    def _report_status(self, now: float) -> None:
        # A gauge's cadence is a real-time concern, not part of the connector's simulated
        # timeline, so this throttle reads the wall clock rather than the injected one: the poll
        # loop can iterate thousands of times a second (poll_timeout 0 in tests), and a Prometheus
        # write per iteration is pure overhead — nothing scrapes faster than seconds. Tests call
        # `_report_lag()` directly.
        real_now = time.monotonic()
        if real_now - self._last_lag_report >= LAG_REPORT_INTERVAL_S:
            self._last_lag_report = real_now
            self._report_lag()
        if self._op:
            self._report("paused", None)
            return
        retrying = [
            st for st in self._parts.values() if st.retry_until is not None and now < st.retry_until
        ]
        if retrying:
            retrying.sort(key=lambda s: s.partition)
            parts = ", ".join(f"{s.partition} ({s.retry_reason})" for s in retrying[:8])
            more = f" and {len(retrying) - 8} more" if len(retrying) > 8 else ""
            self._report("retrying", f"backing off partition {parts}{more}")
        elif self._transient_error is not None:
            self._report("error", self._transient_error)
        else:
            self._report("running", None)

    def _report(self, state: str, detail: str | None) -> None:
        if self._last_status == (state, detail):
            return
        self._last_status = (state, detail)
        try:
            self._status_cb(state, detail)
        except Exception:  # noqa: BLE001 - a status callback never takes the stream down
            logger.debug("dataplane stream %s: status callback failed", self._label)

    def _warn(self, what: str, text: str) -> None:
        """At most one WARNING per :data:`_LOG_EVERY_S` per kind of problem."""
        now = time.monotonic()
        if now - self._last_warning.get(what, float("-inf")) < _LOG_EVERY_S:
            return
        self._last_warning[what] = now
        logger.warning("dataplane stream %s: %s", self._label, text)


def _message_key(msg: Any) -> tuple[str, int] | None:
    """``(topic, partition)`` of a message (an error event may carry neither)."""
    try:
        return (str(msg.topic()), int(msg.partition()))
    except Exception:  # noqa: BLE001 - a partition-less error event
        return None


def _is_header(item: Any, name: str) -> bool:
    return isinstance(item, tuple) and len(item) == 2 and str(item[0]).lower() == name


def _call(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name)()
    except Exception:  # noqa: BLE001 - a foreign error object
        return None


def _error_text(err: Any) -> str:
    text = _call(err, "str")
    return str(text) if text else str(err)


def _result_detail(result: InferenceResult) -> str:
    body = result.body if isinstance(result.body, dict) else {}
    detail = body.get("detail") or body.get("error") or ""
    return str(detail)


# ── the connector ───────────────────────────────────────────────────────────────────────────


def _default_producer_factory(conf: dict[str, Any]) -> Any:
    from confluent_kafka import Producer

    return Producer(conf)


def _default_resolve_connection(name: str, *, project: str | None) -> dict[str, Any]:
    from examlops.connections import resolve_connection

    return resolve_connection(name, project=project, actor="dataplane-streams")


def _default_executor(workers: int) -> _Executor:
    return ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dataplane-kafka")


_PRODUCER_KEYS = ("bootstrap.servers", "security.protocol")


def _topic_option(options: dict[str, Any], name: str) -> str | None:
    value = options.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"options.{name} must be a topic name")
    return value.strip()


class KafkaStreamConnector:
    """The ``kafka`` stream connector (non-singleton: the consumer group balances partitions).

    Every collaborator is injectable for tests: ``dlq`` (default
    :class:`~examlops.dataplane.streams.dlq.LoggingDeadLetterSink`; the supervisor passes the DB
    sink, R15), the consumer/producer factories (default ``confluent_kafka``, imported lazily),
    ``resolve_connection`` (default :func:`examlops.connections.resolve_connection`),
    ``executor_factory`` (default a ``ThreadPoolExecutor``), ``clock`` (monotonic seconds) and
    ``rng`` (backoff jitter).

    One instance may run several streams (one :meth:`run` per stream thread). :meth:`pause` and
    :meth:`resume` are thread-safe: with a binding they target that stream, without one every
    stream this instance runs; a pause also holds for a stream this instance (re)starts later,
    until resumed. A binding whose ``state`` is ``"paused"`` starts paused for that run only —
    the catalog, not this instance, stays the source of truth for it.
    """

    kind = "kafka"
    singleton = False
    connection_kinds = ("kafka",)
    extra = "dataplane-kafka"
    requires = ("confluent_kafka",)

    def __init__(
        self,
        *,
        dlq: DeadLetterSink | None = None,
        consumer_factory: Callable[[dict[str, Any]], Any] | None = None,
        producer_factory: Callable[[dict[str, Any]], Any] | None = None,
        resolve_connection: Callable[..., dict[str, Any]] | None = None,
        executor_factory: Callable[[int], _Executor] | None = None,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
        drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
    ) -> None:
        self._dlq: DeadLetterSink = dlq if dlq is not None else LoggingDeadLetterSink()
        self._consumer_factory = consumer_factory
        self._producer_factory = producer_factory
        self._resolve_connection = resolve_connection or _default_resolve_connection
        self._executor_factory = executor_factory or _default_executor
        self._clock = clock
        self._rng = rng
        self._poll_timeout = poll_timeout_s
        self._drain_timeout = drain_timeout_s
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, str], KafkaStreamSession] = {}
        self._paused: set[tuple[str, str]] = set()
        self._pause_all = False

    # ── the StreamConnector protocol ─────────────────────────────────────────────────────────

    def run(
        self,
        binding: StreamBinding,
        ingress: StreamIngress,
        stop_event: threading.Event,
        status_cb: StatusCallback,
    ) -> None:
        key = (binding.project, binding.name)
        _safe_status(status_cb, "starting", None)
        try:
            session = self.open_session(binding, ingress, status_cb)
        except Exception as exc:
            _safe_status(status_cb, "error", redact_error(f"{type(exc).__name__}: {exc}"))
            raise
        with self._lock:
            self._sessions[key] = session
            # a pause() that raced open_session() must still reach this session
            if self._pause_all or key in self._paused:
                session.set_paused(True)
        try:
            session.run(stop_event)
        finally:
            with self._lock:
                if self._sessions.get(key) is session:
                    del self._sessions[key]

    # ── runtime control (A8b; any thread) ────────────────────────────────────────────────────

    def pause(self, binding: StreamBinding | None = None) -> None:
        with self._lock:
            if binding is None:
                self._pause_all = True
                targets = list(self._sessions.values())
            else:
                key = (binding.project, binding.name)
                self._paused.add(key)
                targets = [s for k, s in self._sessions.items() if k == key]
            for session in targets:
                session.set_paused(True)

    def resume(self, binding: StreamBinding | None = None) -> None:
        with self._lock:
            if binding is None:
                self._pause_all = False
                self._paused.clear()
                targets = list(self._sessions.values())
            else:
                key = (binding.project, binding.name)
                self._paused.discard(key)
                targets = [s for k, s in self._sessions.items() if k == key]
            for session in targets:
                session.set_paused(self._pause_all)

    def is_paused(self, binding: StreamBinding) -> bool:
        key = (binding.project, binding.name)
        with self._lock:
            session = self._sessions.get(key)
            if session is not None:
                return session.paused
            return self._pause_all or key in self._paused

    def session(self, binding: StreamBinding) -> KafkaStreamSession | None:
        with self._lock:
            return self._sessions.get((binding.project, binding.name))

    # ── building a session ───────────────────────────────────────────────────────────────────

    def open_session(
        self, binding: StreamBinding, ingress: StreamIngress, status_cb: StatusCallback
    ) -> KafkaStreamSession:
        """Validate the binding, resolve its connection and build the consumer (and producer).
        Raises :class:`SpecError` / :class:`~examlops.dataplane.types.EgressDenied` before any
        client exists when the binding or its connection is unusable."""
        options = dict(binding.options or {})
        topic = str(binding.address or options.get("topic") or "").strip()
        if not topic:
            raise SpecError(f"kafka stream {binding.name!r} needs a topic (its address)")
        reply_topic = _topic_option(options, "reply_topic")
        dlq_topic = _topic_option(options, "dlq_topic")
        for name, value in (("reply_topic", reply_topic), ("dlq_topic", dlq_topic)):
            if value == topic:
                raise SpecError(f"options.{name} must differ from the stream's own topic")
        start = options.get("start", "earliest")
        if start not in START_POSITIONS:
            raise SpecError(f"options.start must be one of {', '.join(START_POSITIONS)}")

        conn = self._connection(binding)
        secret = conn.get("secret")
        secrets = (str(secret),) if secret else ()
        base = _kafka._checked_conf(conn, None)  # egress-checks every bootstrap host
        consumer_conf = {
            **base,
            "group.id": consumer_group(binding),
            "enable.auto.commit": True,
            "enable.auto.offset.store": False,
            "enable.partition.eof": False,
            "auto.offset.reset": "earliest",  # out-of-range safety; `start` is applied on assign
            "client.id": f"examlops-dataplane-{binding.name}"[:200],
        }
        producer_conf: dict[str, Any] | None = None
        if reply_topic or dlq_topic:
            producer_conf = {
                k: v for k, v in base.items() if k in _PRODUCER_KEYS or k.startswith("sasl.")
            }
            producer_conf.update(
                {
                    "enable.idempotence": True,
                    "acks": "all",
                    "client.id": f"examlops-dataplane-{binding.name}"[:200],
                }
            )
        with self._lock:
            key = (binding.project, binding.name)
            paused = binding.state == "paused" or self._pause_all or key in self._paused
        return KafkaStreamSession(
            binding,
            ingress,
            consumer_factory=self._consumer_factory or _kafka._default_factory,
            consumer_conf=consumer_conf,
            producer_factory=(
                (self._producer_factory or _default_producer_factory) if producer_conf else None
            ),
            producer_conf=producer_conf,
            topic=topic,
            reply_topic=reply_topic,
            dlq_topic=dlq_topic,
            dlq=self._dlq,
            status_cb=status_cb,
            executor_factory=self._executor_factory,
            clock=self._clock,
            rng=self._rng,
            poll_timeout_s=self._poll_timeout,
            drain_timeout_s=self._drain_timeout,
            secrets=secrets,
            paused=paused,
            start=start,
        )

    def _connection(self, binding: StreamBinding) -> dict[str, Any]:
        if not binding.connection:
            raise SpecError(f"kafka stream {binding.name!r} needs a kafka Named Connection")
        try:
            conn = self._resolve_connection(binding.connection, project=binding.project or None)
        except ConnectionError as exc:
            raise SpecError(str(exc)) from None
        kind = conn.get("kind")
        if kind not in self.connection_kinds:
            raise SpecError(
                f"connection {binding.connection!r} is a {kind!r} connection; "
                "a kafka stream needs a kafka connection"
            )
        return conn


def _safe_status(status_cb: StatusCallback, state: str, detail: str | None) -> None:
    try:
        status_cb(state, detail)
    except Exception:  # noqa: BLE001
        logger.debug("stream status callback failed", exc_info=True)


__all__ = [
    "ANSWERED",
    "BACKOFF_BASE_S",
    "BACKOFF_CAP_S",
    "DLQ_HEADERS",
    "EXHAUSTED_OUTCOME",
    "EnvelopeRejected",
    "EXPIRED_OUTCOME",
    "IDEMPOTENCY_KEY_MAX",
    "LOCAL_SHED_REASONS",
    "LOCAL_SHED_RETRY_AFTER_S",
    "MAX_WORKERS",
    "REJECTED_OUTCOME",
    "REASON_EXPIRED",
    "RETRYABLE",
    "KafkaStreamConnector",
    "KafkaStreamSession",
    "backoff_delay",
    "consumer_group",
    "encode_reply",
    "is_local_shed",
    "parse_value",
    "read_idempotency_key",
    "read_traceparent",
]
