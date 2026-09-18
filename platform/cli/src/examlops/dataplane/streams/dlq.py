"""Dead letters for asynchronous stream connectors (ADR 0131 §5, Plan 2, rulings R3/R15, E6).

A connector with no waiting caller — Kafka today, SeanerBUS pub/sub later — parks a message it
cannot process through a :class:`DeadLetterSink`. This module holds the seam — the Protocol, the
reason vocabulary, the error-text redaction every sink and header must use, and
:class:`LoggingDeadLetterSink`, the metadata-only default — and the database-backed store behind
it (task A7b): :class:`DbDeadLetterSink` writes ``dataplane_stream_dead_letters``;
:func:`list_dead_letters`, :func:`get_dead_letter`, :func:`replay`, :func:`purge` and
:func:`prune` read and manage it.

**What is stored.** Always the metadata: reason, the error (redacted: :func:`redact_error`, then
the guardrails PII redaction, at most :data:`STORED_ERROR_MAX_CHARS` characters — a validation
detail can echo payload keys and a model detail input fragments), attempts, the SHA-256 and size
of the *original* bytes, and the origin (Kafka topic/partition/offset). The payload itself only
when the binding opts in (``options.dlq_store_payload``, or the sink's
``dlq_store_payload_default``), only up to :data:`PAYLOAD_MAX_BYTES` (a larger one is recorded as
``payload_truncated`` with no payload — never a partial one), only when it is UTF-8 (otherwise
``payload_encoding='binary-dropped'``), and only after secrets (:func:`examlops.dataplane.safety.
redact`) and personal data (:func:`examlops.guardrails.redact_pii`) are masked. A JSON payload is
redacted value by value, so it stays JSON and can be replayed; any other text is redacted whole.
A secret-shaped key (:func:`examlops.dataplane.safety.is_secret_key`) masks its **whole subtree**,
whatever its shape, and a document nested deeper than :data:`_MAX_PAYLOAD_DEPTH` is not stored at
all (``payload_encoding='too-deep-dropped'``) — the subtree rule lives in the structured walk, so
a payload that walk cannot handle is refused rather than kept under a weaker redaction. Redaction
is signature-based, so a credential that reaches the store encoded (base64 inside an innocuous
key, say) is not recognised: ``dlq_store_payload`` is a considered opt-in, not a guarantee that
nothing sensitive is ever kept.

**Idempotent on the origin** (R15). A redelivered message — a crash between ``record()`` and the
offset store — updates its row rather than adding one: ``attempts`` becomes the larger value and
``reason``/``error`` are replaced while the row is not yet replayed. A record with no complete
origin (a future push source with no offset) is never deduplicated.

**Failure.** :meth:`DbDeadLetterSink.record` raises when the database write fails: the Kafka
connector then stores no offset and retries the write, so a dead letter is never lost.

**Audit** (the tamper-evident log): ``dataplane_stream_dlq_replayed`` (id, outcome, actor —
never the payload), ``dataplane_stream_dlq_replay_lost`` when a replay's claim was taken over
while it ran and its write therefore changed nothing, ``dataplane_stream_dlq_purged`` (count) and,
once per retention run that deleted anything, ``dataplane_stream_dlq_pruned``. Recording a dead
letter is not audited (the Plan 2 rule: no per-message audit row); it is counted in
``dataplane_stream_dead_letters_total{project,stream,reason}``.

**Tenancy.** Every read takes the project and filters on it; an id of another project reads
exactly like an id that does not exist.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets as _secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from examlops.dataplane.safety import is_secret_key, redact
from examlops.dataplane.streams import metrics
from examlops.dataplane.streams.types import (
    InferenceResult,
    StreamBinding,
    StreamRequest,
    display_project,
)
from examlops.dataplane.types import SpecError

logger = logging.getLogger(__name__)

#: Byte ceiling on any dead-letter error text (a Kafka header, a log line).
ERROR_MAX_BYTES = 512

#: Reasons a message is dead-lettered. Ingress outcomes that dead-letter at once (``validation``,
#: ``not_found``, ``unexpected``) keep the outcome's own name.
REASON_OVERSIZE = "oversize"
REASON_NOT_JSON = "not_json"
REASON_INVALID_MESSAGE = "invalid_message"
REASON_RETRIES_EXHAUSTED = "retries_exhausted"

#: The largest payload a dead letter stores (of the original bytes). Larger: metadata only.
PAYLOAD_MAX_BYTES = 256 * 1024
#: Ceiling on a stored error, in characters, after redaction.
STORED_ERROR_MAX_CHARS = 1024
#: ``payload_encoding`` of a stored payload, of an opted-in payload that is not UTF-8, and of one
#: whose redaction failed (never stored unredacted).
ENCODING_UTF8 = "utf-8"
ENCODING_BINARY_DROPPED = "binary-dropped"
ENCODING_REDACTION_FAILED = "redaction-failed"
ENCODING_TOO_DEEP_DROPPED = "too-deep-dropped"
#: Deepest JSON nesting a stored payload may have. The same value as
#: ``examlops.dataplane.streams.schema._MAX_PAYLOAD_DEPTH``, which the ingress's payload walk
#: already enforces — repeated rather than imported, because that name is private to that module.
#: Deeper than this and the payload is not stored at all (``ENCODING_TOO_DEEP_DROPPED``): the
#: structured scrubber is what applies the secret-shaped-key rule, and a fallback that redacts by
#: pattern does not know a key's shape.
_MAX_PAYLOAD_DEPTH = 32
#: Retention of dead letters, in days (the dataplane scheduler prunes older ones).
RETENTION_ENV = "EXAMLOPS_DATAPLANE_DLQ_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 7
#: A replay claim older than this belongs to a replayer that died; another replay may take it.
REPLAY_CLAIM_STALE_S = 600
#: Outcomes that end a replay: the model answered (``ok``) or failed on the input (``model``).
REPLAY_DONE_OUTCOMES = frozenset({"ok", "model"})
#: ``body["error"]`` of the result a replay returns when another replay took over its claim.
REPLAY_CLAIM_LOST = "replay_claim_lost"

_MASK = "***"
_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_REASON_MAX_CHARS = 64
_LIST_LIMIT_MAX = 1000


def redact_error(
    text: object, *, max_bytes: int = ERROR_MAX_BYTES, secrets: Iterable[str] = ()
) -> str:
    """``text`` through :func:`examlops.dataplane.safety.redact`, then cut to ``max_bytes`` of
    UTF-8 without splitting a character."""
    cleaned = redact(str(text), secrets=[s for s in secrets if s])
    raw = cleaned.encode("utf-8")
    if len(raw) <= max_bytes:
        return cleaned
    return raw[:max_bytes].decode("utf-8", "ignore")


class DeadLetterSink(Protocol):
    """Where an asynchronous connector parks a message it cannot process.

    ``reason`` is one of the ``REASON_*`` constants or a dead-lettering ingress outcome; ``error``
    is already redacted and bounded; ``attempts`` counts processing attempts; ``payload`` is the
    message's raw bytes (``None`` when the message had none); ``origin`` locates it at its source
    (for Kafka ``{"topic", "partition", "offset"}``) and identifies a redelivered dead letter.
    Raising means the dead letter was **not** written: the Kafka connector stores no offset for the
    message and retries the write after a backoff (ruling R16), so a sink must raise on a real
    storage failure rather than swallow it.
    """

    def record(
        self,
        binding: StreamBinding,
        *,
        reason: str,
        error: str,
        attempts: int,
        payload: bytes | None,
        origin: dict[str, Any],
    ) -> None: ...


class LoggingDeadLetterSink:
    """The default sink: one WARNING line of metadata. Never the payload."""

    def record(
        self,
        binding: StreamBinding,
        *,
        reason: str,
        error: str,
        attempts: int,
        payload: bytes | None,
        origin: dict[str, Any],
    ) -> None:
        size = len(payload) if payload is not None else 0
        digest = hashlib.sha256(payload).hexdigest() if payload is not None else "-"
        logger.warning(
            "dataplane stream %s/%s: dead letter reason=%s attempts=%d size=%d sha256=%s "
            "origin=%s error=%s",
            display_project(binding.project),
            binding.name,
            reason,
            attempts,
            size,
            digest,
            json.dumps(origin, sort_keys=True, default=str),
            redact_error(error),
        )


# ── payload and error preparation (pure) ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PreparedPayload:
    """What a dead letter stores of its message: always the digest and size of the original
    bytes; ``text`` only when the payload was opted in, small enough, UTF-8, and redacted."""

    sha256: str | None
    size: int
    text: str | None = None
    encoding: str | None = None
    truncated: bool = False


def store_payload_enabled(options: Any, default: bool = False) -> bool:
    """Whether a binding's ``options.dlq_store_payload`` opts in to storing payloads.

    Strict, because it is a privacy opt-in: ``True``, ``1`` or one of ``1``/``true``/``yes``/
    ``on`` (any case) enable it; any other value present — ``"false"``, ``"no"``, ``0``, junk —
    disables it. Only an absent key falls back to ``default``.
    """
    if not isinstance(options, dict) or "dlq_store_payload" not in options:
        return bool(default)
    value = options["dlq_store_payload"]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_STRINGS
    return False


def _scrub_text(text: str) -> str:
    """Secrets, then personal data, masked in one string."""
    from examlops.guardrails import redact_pii

    return redact_pii(redact(text))[0]


def _scrub_value(value: Any) -> Any:
    """A parsed JSON value with every string redacted, and an integer whose digits read as
    personal data (a phone number, a card) replaced by the redacted text; floats, booleans and
    ``null`` are left alone.

    A **secret-shaped key** (:func:`examlops.dataplane.safety.is_secret_key`) masks its whole
    subtree — the value becomes ``***`` whatever its shape: a string, a number, a list of
    credentials, a list of objects holding them, or a nested object. Review finding C1: masking
    only an immediate string child left ``{"api_keys": [<key>, <key>]}`` intact, because the
    elements were then scrubbed as standalone strings and the textual ``key=value`` patterns
    :func:`redact` looks for do not fire on a bare opaque token. Masking the subtree also hides
    its shape (a list's length, an object's keys), which is what "redacted" should mean for
    something a project shares.
    """
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, bool) or value is None or isinstance(value, float):
        return value
    if isinstance(value, int):
        digits = str(value)
        scrubbed = _scrub_text(digits)
        return value if scrubbed == digits else scrubbed
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            out[_scrub_text(name)] = _MASK if is_secret_key(name) else _scrub_value(item)
        return out
    return _scrub_text(str(value))  # pragma: no cover - json.loads yields only the types above


def json_depth_exceeds(doc: Any, limit: int = _MAX_PAYLOAD_DEPTH) -> bool:
    """Does ``doc`` nest containers deeper than ``limit``?

    Iterative, with an explicit stack: measuring a 20 000-level document must not be the thing
    that raises :class:`RecursionError`. Depth counts containers — the outermost object or array
    is depth 0 — so ``limit`` nested containers pass and one more does not.
    """
    stack: list[tuple[Any, int]] = [(doc, 0)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, dict):
            if depth >= limit:
                return True
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            if depth >= limit:
                return True
            stack.extend((item, depth + 1) for item in value)
    return False


def redact_payload_text(text: str) -> str | None:
    """``text`` with secrets and personal data masked, or ``None`` when it must not be stored.

    A JSON document is redacted value by value and re-serialised, so the result is still JSON and
    can be replayed. Text that is not JSON is redacted whole, by pattern.

    ``None`` means "drop the payload", and it is what a document nested deeper than
    :data:`_MAX_PAYLOAD_DEPTH` gets (re-review of finding C1): the pattern redaction that used to
    catch the walk's ``RecursionError`` does not know a key's shape, so a 20 000-level document —
    still well under 256 KiB, brackets being cheap — could smuggle an array of credentials under a
    secret-shaped key past the subtree rule. Nothing that deep is worth keeping, and the ingress
    would refuse it anyway. The ``RecursionError``/``ValueError`` handlers below are therefore
    unreachable for a document that got this far; they stay as a last resort and they drop the
    payload too, rather than falling back to the weaker redaction.
    """
    try:
        doc = json.loads(text)
    except RecursionError:  # too deep for the parser itself: never stored
        return None
    except ValueError:
        return _scrub_text(text)  # not JSON: pattern redaction over the whole text
    if json_depth_exceeds(doc):
        return None
    try:
        scrubbed = _scrub_value(doc)
        out = json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":"))
    except (RecursionError, ValueError):
        return None
    try:
        out.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate from a \\ud800 escape: keep it escaped
        out = json.dumps(scrubbed, ensure_ascii=True, separators=(",", ":"))
    return out


def prepare_payload(payload: bytes | None, *, store: bool) -> PreparedPayload:
    """What a dead letter keeps of ``payload`` (see :class:`PreparedPayload`). Never raises: a
    redaction failure stores no payload (``payload_encoding='redaction-failed'``), and neither
    does a document nested deeper than :data:`_MAX_PAYLOAD_DEPTH` (``'too-deep-dropped'``). The
    digest, size and origin are recorded either way."""
    if payload is None:
        return PreparedPayload(sha256=None, size=0)
    raw = bytes(payload)
    digest, size = hashlib.sha256(raw).hexdigest(), len(raw)
    if not store:
        return PreparedPayload(sha256=digest, size=size)
    if size > PAYLOAD_MAX_BYTES:
        return PreparedPayload(sha256=digest, size=size, truncated=True)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return PreparedPayload(sha256=digest, size=size, encoding=ENCODING_BINARY_DROPPED)
    if "\x00" in text:  # valid UTF-8, but not text a database column holds (Postgres refuses it)
        return PreparedPayload(sha256=digest, size=size, encoding=ENCODING_BINARY_DROPPED)
    try:
        redacted = redact_payload_text(text)
    except Exception as exc:  # noqa: BLE001 - never store an unredacted payload, never block
        logger.warning("dataplane dead letter: payload redaction failed (%s)", type(exc).__name__)
        return PreparedPayload(sha256=digest, size=size, encoding=ENCODING_REDACTION_FAILED)
    if redacted is None:  # nested past the cap: refused, not stored under a weaker redaction
        return PreparedPayload(sha256=digest, size=size, encoding=ENCODING_TOO_DEEP_DROPPED)
    return PreparedPayload(sha256=digest, size=size, text=redacted, encoding=ENCODING_UTF8)


def _column_text(value: object) -> str:
    """``str(value)`` with lone surrogates and NUL replaced by ``?`` — text both backends store
    (SQLite cannot encode a surrogate, Postgres refuses NUL), so a write never fails on it."""
    return str(value).encode("utf-8", "replace").decode("utf-8").replace("\x00", "?")


def stored_error(error: object) -> str:
    """The error text a dead-letter row keeps: :func:`redact_error`, then personal data masked,
    at most :data:`STORED_ERROR_MAX_CHARS` characters. NUL characters and lone surrogates — text
    no database column takes, so a write would fail on every retry — are replaced first."""
    text = _column_text(error)
    # 4 bytes per character at most, so this byte cut never shortens below the character cap.
    cleaned = redact_error(text, max_bytes=4 * STORED_ERROR_MAX_CHARS)
    try:
        from examlops.guardrails import redact_pii

        cleaned = redact_pii(cleaned)[0]
    except Exception as exc:  # noqa: BLE001 - the secret-redacted text is still safe to keep
        logger.warning("dataplane dead letter: error PII redaction failed (%s)", type(exc).__name__)
    return cleaned[:STORED_ERROR_MAX_CHARS]


def origin_key(origin: Any) -> tuple[str | None, int | None, int | None]:
    """``(topic, partition, offset)`` of an origin that has all three, else ``(None,)*3`` —
    no deduplication for it."""
    if not isinstance(origin, dict):
        return None, None, None
    topic, partition, offset = origin.get("topic"), origin.get("partition"), origin.get("offset")
    if (
        isinstance(topic, str)
        and topic
        and isinstance(partition, int)
        and not isinstance(partition, bool)
        and isinstance(offset, int)
        and not isinstance(offset, bool)
    ):
        return topic, partition, offset
    return None, None, None


# ── the database sink ───────────────────────────────────────────────────────────────────────


class DbDeadLetterSink:
    """The :class:`DeadLetterSink` the stream supervisor passes to the connectors: one row per
    dead letter in ``dataplane_stream_dead_letters`` (see the module docstring).

    ``dlq_store_payload_default`` applies to a binding whose ``options`` do not set
    ``dlq_store_payload``. :meth:`record` raises when the database write fails.
    """

    def __init__(self, *, dlq_store_payload_default: bool = False) -> None:
        self._store_default = bool(dlq_store_payload_default)

    def record(
        self,
        binding: StreamBinding,
        *,
        reason: str,
        error: str,
        attempts: int,
        payload: bytes | None,
        origin: dict[str, Any],
    ) -> None:
        from examlops.data import dataplane as catalog

        prepared = prepare_payload(
            payload,
            store=store_payload_enabled(getattr(binding, "options", None), self._store_default),
        )
        topic, partition, offset = origin_key(origin)
        reason_text = _column_text(reason)[:_REASON_MAX_CHARS]
        _id, created = catalog.upsert_dead_letter(
            binding.project,
            binding.name,
            reason=reason_text,
            error=stored_error(error),
            attempts=max(0, int(attempts)),
            sha256=prepared.sha256,
            size=prepared.size,
            origin=dict(origin) if isinstance(origin, dict) else {},
            origin_topic=topic,
            origin_partition=partition,
            origin_offset=offset,
            payload=prepared.text,
            payload_encoding=prepared.encoding,
            payload_truncated=prepared.truncated,
        )
        if created:
            metrics.dead_letter(binding.project, binding.name, reason_text)


# ── reading, replay, purge, prune ───────────────────────────────────────────────────────────


class _Ingress(Protocol):
    def handle(self, binding: StreamBinding, req: StreamRequest) -> InferenceResult: ...


def _as_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


def _label(project: str, stream: str | None) -> str:
    return f"{display_project(project)}/{stream or '*'}"


def _utc_text(moment: datetime) -> str:
    """``YYYY-MM-DD HH:MM:SS`` UTC — the text ``CURRENT_TIMESTAMP`` writes on both backends."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def list_dead_letters(
    project: str,
    *,
    stream: str | None = None,
    limit: int = 50,
    reason: str | None = None,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    """``project``'s dead letters (optionally one stream), newest first, without payloads
    (``has_payload`` says whether one is stored). ``limit`` is clamped to 1…1000.

    ``reason`` keeps only that failure reason and ``before_id`` only rows older than that id; both
    narrow the query itself, so ``limit`` counts rows the caller asked for rather than rows the
    caller is about to discard.
    """
    from examlops.data import dataplane as catalog

    return catalog.list_dead_letter_rows(
        project,
        stream=stream,
        limit=max(1, min(int(limit), _LIST_LIMIT_MAX)),
        reason=reason,
        before_id=before_id,
    )


def get_dead_letter(project: str, dead_letter_id: Any) -> dict[str, Any] | None:
    """One dead letter of ``project``, payload included; ``None`` for an unknown id, a malformed
    one, or an id that belongs to another project — the three are indistinguishable."""
    from examlops.data import dataplane as catalog

    number = _as_id(dead_letter_id)
    if number is None:
        return None
    return catalog.get_dead_letter_row(project, number)


def claim_lost_result(dead_letter_id: int) -> InferenceResult:
    """What :func:`replay` returns when its claim was taken over while it ran: the row belongs to
    the replay that owns it now, and this attempt's own result is discarded, not reported as the
    dead letter's outcome."""
    return InferenceResult(
        outcome="unexpected",
        body={
            "error": REPLAY_CLAIM_LOST,
            "detail": (
                f"another replay took over dead letter {dead_letter_id} while this one ran; "
                "its result was discarded"
            ),
        },
        status=409,
    )


def replay(
    dead_letter_id: Any,
    *,
    actor: str | None,
    ingress: _Ingress,
    binding: StreamBinding,
    force: bool = False,
) -> InferenceResult:
    """Re-offer a stored dead letter to ``binding``'s ingress; the ingress's result.

    The request is built from the stored payload by the Kafka connector's own envelope parser
    (:func:`examlops.dataplane.streams.kafka_stream.parse_value`), which raises
    :class:`~examlops.dataplane.streams.kafka_stream.EnvelopeRejected` for a payload it would have
    rejected. An ``ok`` or ``model`` outcome marks the row replayed (``replayed_at``/
    ``replayed_by``); any other outcome leaves it replayable. Every attempt that reaches the
    parser is audited as ``dataplane_stream_dlq_replayed`` — id, outcome, actor, never the payload.

    **A lost claim** (review finding I1): a replay slow enough for its claim to go stale can find
    that another replay took it over and finished the row. Its own write then changes nothing, so
    it must not claim in the audit log that it did: the row is left exactly as the replay that
    owns it left it, the event is ``dataplane_stream_dlq_replay_lost`` (id, stream, the outcome
    this attempt saw, the actor), and the return value is :func:`claim_lost_result` — outcome
    ``unexpected`` with ``body["error"] = "replay_claim_lost"`` — not the discarded result.

    Raises :class:`SpecError` for an id that is unknown, malformed or not a dead letter of
    ``binding``'s project and stream (one message for all of them), for a dead letter with no
    stored payload, for one already replayed (unless ``force``), and while another replay of the
    same dead letter is running.
    """
    from examlops.data import dataplane as catalog
    from examlops.data.audit import write_audit_event
    from examlops.dataplane.streams.kafka_stream import EnvelopeRejected, parse_value

    project, stream = binding.project, binding.name
    row = get_dead_letter(project, dead_letter_id)
    if row is None or row["stream"] != stream:
        raise SpecError(f"dead letter {dead_letter_id} not found")
    dl_id = int(row["id"])
    if row.get("payload") is None:
        raise SpecError(
            f"dead letter {dl_id} has no stored payload; re-publish the original message"
        )
    if row.get("replayed_at") and not force:
        raise SpecError(
            f"dead letter {dl_id} was already replayed at {row['replayed_at']}; "
            "replay it again with force"
        )
    claim = _secrets.token_hex(8)
    stale_before = _utc_text(datetime.now(UTC) - timedelta(seconds=REPLAY_CLAIM_STALE_S))
    if not catalog.claim_dead_letter_replay(
        project, dl_id, claim, force=force, stale_before=stale_before
    ):
        raise SpecError(
            f"dead letter {dl_id} is being replayed, or was just replayed; try again later"
        )

    details: dict[str, Any] = {"id": dl_id, "stream": stream, "force": bool(force)}
    replayed_by: str | None = None
    result: InferenceResult | None = None
    kept_claim = False
    try:
        try:
            doc, model, alias, metadata = parse_value(
                str(row["payload"]).encode("utf-8"), max_bytes=int(binding.limits.max_bytes)
            )
        except EnvelopeRejected as reject:
            details.update(outcome="rejected", reason=reject.reason, replayed=False)
            raise
        metadata.pop("tenant", None)  # defence in depth; the ingress never reads it (I4)
        request = StreamRequest(
            stream=stream, model=model, alias=alias, payload=doc, metadata=metadata
        )
        result = ingress.handle(binding, request)
        done = result.outcome in REPLAY_DONE_OUTCOMES
        replayed_by = (actor or "unknown") if done else None
        details.update(outcome=result.outcome, replayed=done)
    finally:
        if "outcome" not in details:  # the ingress raised: nothing was decided
            details.update(outcome="error", replayed=False)
        try:
            # False: the claim went stale and another replay took it over, so this write changed
            # nothing — the audit event must say so rather than assert a state change (I1).
            kept_claim = catalog.finish_dead_letter_replay(
                project, dl_id, claim, replayed_by=replayed_by
            )
        except Exception as exc:  # noqa: BLE001 - re-review: the same hazard M6 named, one call
            # up. Raising out of this `finally` would REPLACE whatever the body raised (the
            # `EnvelopeRejected`, or the ingress's own error) with a datastore error, turning a
            # classifiable failure into an opaque one. The claim is left to go stale instead — a
            # later replay takes it over after REPLAY_CLAIM_STALE_S — and the caller still gets
            # the real answer. `kept_claim` stays False, so the audit row says "lost", which is
            # the truth: this attempt did not record a completion.
            logger.warning(
                "dataplane stream %s: could not finish the dead-letter replay claim of %d (%s); "
                "the claim is left to go stale",
                _label(project, stream),
                dl_id,
                type(exc).__name__,
            )
        finally:
            try:
                write_audit_event(
                    "dataplane",
                    actor,
                    "dataplane_stream_dlq_replayed"
                    if kept_claim
                    else "dataplane_stream_dlq_replay_lost",
                    _label(project, stream),
                    details if kept_claim else {**details, "replayed": False},
                )
            except Exception as exc:  # noqa: BLE001 - review M6: an audit failure in this
                # `finally` used to REPLACE whatever the body raised (an `EnvelopeRejected`, the
                # ingress's own error), turning a classifiable failure into an opaque one. The
                # audit row is the loss here, and it is the lesser one.
                logger.warning(
                    "dataplane stream %s: could not audit the dead-letter replay of %d (%s)",
                    _label(project, stream),
                    dl_id,
                    type(exc).__name__,
                )
    if not kept_claim or result is None:  # result is None only if the try raised, which returns
        return claim_lost_result(dl_id)
    return result


def purge(project: str, stream: str | None, *, older_than: timedelta, actor: str | None) -> int:
    """Delete ``project``'s dead letters of ``stream`` (``None``: every stream of the project)
    created more than ``older_than`` ago; the count. Audited as ``dataplane_stream_dlq_purged``
    with the count, also when it is zero (an operator's action)."""
    from examlops.data import dataplane as catalog
    from examlops.data.audit import write_audit_event

    if older_than < timedelta(0):
        raise ValueError("older_than must not be negative")
    before = _utc_text(datetime.now(UTC) - older_than)
    count = catalog.purge_dead_letters(project, stream, before=before)
    write_audit_event(
        "dataplane",
        actor,
        "dataplane_stream_dlq_purged",
        _label(project, stream),
        {"count": count, "older_than_s": int(older_than.total_seconds())},
    )
    return count


def retention_days() -> int:
    """``EXAMLOPS_DATAPLANE_DLQ_RETENTION_DAYS``: a positive integer, else the default (7)."""
    raw = os.getenv(RETENTION_ENV, "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError:
        days = 0
    if days < 1:
        logger.warning(
            "%s=%r is not a positive number of days; using %d",
            RETENTION_ENV,
            raw,
            DEFAULT_RETENTION_DAYS,
        )
        return DEFAULT_RETENTION_DAYS
    return days


def prune(*, days: int | None = None, actor: str | None = "dataplane-scheduler") -> int:
    """Retention: delete every project's dead letters older than ``days`` (default
    :func:`retention_days`); the count. One ``dataplane_stream_dlq_pruned`` audit event per run
    that deleted anything."""
    from examlops.data import dataplane as catalog
    from examlops.data.audit import write_audit_event

    keep = retention_days() if days is None else int(days)
    if keep < 1:
        raise ValueError("retention must be at least one day")
    before = _utc_text(datetime.now(UTC) - timedelta(days=keep))
    count = catalog.prune_dead_letters(before=before)
    if count > 0:
        write_audit_event(
            "dataplane",
            actor,
            "dataplane_stream_dlq_pruned",
            "dead-letters",
            {"count": count, "retention_days": keep},
        )
    return count


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "ENCODING_BINARY_DROPPED",
    "ENCODING_REDACTION_FAILED",
    "ENCODING_TOO_DEEP_DROPPED",
    "ENCODING_UTF8",
    "ERROR_MAX_BYTES",
    "PAYLOAD_MAX_BYTES",
    "REASON_INVALID_MESSAGE",
    "REASON_NOT_JSON",
    "REASON_OVERSIZE",
    "REASON_RETRIES_EXHAUSTED",
    "REPLAY_CLAIM_LOST",
    "RETENTION_ENV",
    "STORED_ERROR_MAX_CHARS",
    "DbDeadLetterSink",
    "DeadLetterSink",
    "LoggingDeadLetterSink",
    "PreparedPayload",
    "claim_lost_result",
    "get_dead_letter",
    "json_depth_exceeds",
    "list_dead_letters",
    "origin_key",
    "prepare_payload",
    "prune",
    "purge",
    "redact_error",
    "redact_payload_text",
    "replay",
    "retention_days",
    "store_payload_enabled",
    "stored_error",
]
