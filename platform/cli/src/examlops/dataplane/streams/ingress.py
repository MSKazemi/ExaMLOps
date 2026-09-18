"""Stream ingress orchestration (ADR 0130/0131, Plan 2, task A5).

:class:`StreamIngress` is the one path every live-stream request takes, whichever connector
delivered it (HTTP push, SeanerBUS req/res, Kafka)::

    model check → validate + build body → in-flight permit → rate check → infer
        → idempotency check (ok/model only) → reply → offer telemetry (ok only) → feed drift

Properties it keeps, each carried from the SeanerBUS bridge or ruled by the controller:

* **The binding's model and alias are authoritative** (M1, and C1 of the final review). The
  binding is the authorization unit, so a request naming a different model — or a different
  ``alias`` — is refused as ``validation`` (422, "this stream is bound to …"): the ingress never
  infers against a caller-chosen model or alias, and a caller can never mint ``model`` label
  values or choose which ``(model, alias)`` drift window its predictions land in. The alias
  decides both which *version* serves the request and which drift window feeds
  ``exa drift status``/``drift_auto_retrain``/autopilot, so a message choosing it would let a
  canary producer push a Production window over its threshold.

  One opt-in escape: a binding whose ``options["allow_alias_override"]`` is exactly ``True``
  accepts a caller-supplied alias — and even then only one of :data:`KNOWN_ALIASES`
  (``examlops.cli._enums.MLflowAlias``: Production, Canary, Staging), so the value written to
  ``drift_snapshots.alias``/``input_snapshots.alias`` stays bounded. Anything else is a
  ``validation`` result.
* **A message may never name its own tenant** (I4). Every connector strips ``metadata["tenant"]``,
  but the rule is enforced where the value is *read*: :meth:`StreamIngress._offer_telemetry`
  takes the tenant from the binding's deployment (``None`` in S1 — a stream's tenancy unit is its
  ``project``, which the record carries) and never from the request, so a connector that arrives
  later as pack content cannot re-open the hole by forgetting to strip it.
* **Never raises into the caller.** Every failure — an invalid payload, a full stream, a rate
  cap, an inference error, a client bug — is an :class:`IngressResult`. A validation failure is a
  ``validation`` result (422) whose detail names the field, never echoes the payload.
* **Reply first.** ``handle(..., reply=callback)`` calls ``reply(result)`` *before* anything
  telemetry-related runs, and ``handle`` returns the same result. Telemetry is an
  ``offer()`` to a non-blocking, drop-on-full spool; drift is an O(1) in-memory
  :meth:`DriftAggregator.observe` whose trigger work runs on the aggregator's own background
  executor. Neither can add latency to the reply or raise into the caller.
* **Telemetry for ``ok`` only** (M8, bridge parity R2): the bridge offered telemetry from its
  success path only, and the DB sink persists nothing else — a failure record would only take
  spool capacity during the very outages that fill it.
* **Model failures feed drift; nothing else does.** ``model`` observes a failure and ``ok`` a
  success (bridge parity: a served answer with no prediction is a failure); validation, shed,
  transport, deadline, not-found and unexpected outcomes never reach the aggregator.
* **Backpressure.** A per-stream bounded semaphore (``limits.max_in_flight``), acquired
  non-blocking: full → ``overloaded`` with ``retry_after=1`` (shed reason ``in_flight``). A
  ``limits.rate_per_min`` cap via ``coord.allow`` → ``overloaded`` with ``retry_after=60/rate``
  capped at 60 (shed reason ``rate``). The coordinator is only touched when a cap is set, or an
  idempotency key is present — the default hot path writes nothing to ``platform.db``.
* **Coordinator failure fails open** (logged at WARNING, at most once a minute per check): an
  unanswered rate check admits, an unanswered idempotency check counts as first-seen.
* **Idempotency (E18, M2).** The key ``dataplane:stream:{project}:{stream}:{key}`` (TTL 600 s) is
  checked *after* inference and only for an ``ok`` or ``model`` outcome — the outcomes that are
  counted. A first attempt that was shed, or failed in transport or on its deadline, therefore
  never consumes the key, and its retry is counted normally. A replay is still served, but skips
  telemetry and drift, and its result says ``replayed=True``.

Result type: :class:`IngressResult` is a frozen **subclass** of A2's ``InferenceResult`` adding
``replayed`` — every consumer typed against ``InferenceResult`` keeps working, and ``types.py``
stays untouched.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from typing import Any

from examlops.cli._enums import MLflowAlias
from examlops.coordination import Coordinator
from examlops.dataplane.streams import metrics
from examlops.dataplane.streams.client import (
    CALLER_STATUS,
    InferenceClient,
    effective_budget_ms,
)
from examlops.dataplane.streams.drift import DriftAggregator
from examlops.dataplane.streams.schema import ModelSchemaRegistry, build_body, validate
from examlops.dataplane.streams.telemetry import EmbeddingStats, TelemetryRecord, TelemetrySpool
from examlops.dataplane.streams.types import (
    InferenceResult,
    Outcome,
    StreamBinding,
    StreamRequest,
)
from examlops.dataplane.types import SpecError

logger = logging.getLogger(__name__)

#: ``IngressResult.shed_reason`` values that mark the ingress's own backpressure, as opposed to an
#: ``overloaded`` the model service itself reported. The ingress stamps them, so this is where they
#: are defined; :mod:`examlops.dataplane.streams.kafka_stream` re-exports the name it already
#: published, and the service's push route imports it rather than re-spelling the two strings
#: (review M2).
LOCAL_SHED_REASONS = frozenset({"in_flight", "rate"})

#: Seconds a replayed idempotency key is remembered (E18).
IDEMPOTENCY_TTL_S = 600.0
#: ``Retry-After`` for a request shed because the stream's in-flight permits are all taken.
IN_FLIGHT_RETRY_AFTER_S = 1.0
_RATE_WINDOW_S = 60.0
_DETAIL_MAX = 300
_LOG_EVERY_S = 60.0
#: The outcomes that are counted — they reach telemetry/drift and consume an idempotency key.
_COUNTED = frozenset({"ok", "model"})
#: The binding option that lets a caller name the alias (C1). Only ``True`` opts in.
ALIAS_OVERRIDE_OPTION = "allow_alias_override"
#: The alias names the platform knows (``exa`` uses the same enum for ``--alias``). Even with the
#: override opted in, an alias outside this set is refused: the value reaches two platform tables.
KNOWN_ALIASES = frozenset(a.value for a in MLflowAlias)


def is_local_shed(result: InferenceResult) -> bool:
    """``True`` only for a request the ingress shed *itself* — :data:`LOCAL_SHED_REASONS`."""
    return getattr(result, "shed_reason", None) in LOCAL_SHED_REASONS


def resolve_alias(binding: StreamBinding, req: StreamRequest) -> str | None:
    """The alias this request is served with, or ``None`` when the request named another one (C1).

    The binding's alias is authoritative, exactly as its model is (M1): a message may not choose
    which model *version* answers it, nor which ``(model, alias)`` drift window its prediction
    lands in. A request naming nothing (``""``) takes the binding's alias, and one naming the
    binding's own alias is fine.

    ``binding.options["allow_alias_override"] is True`` opts one stream in — for a genuinely
    multi-alias producer — and even then the caller may only name one of :data:`KNOWN_ALIASES`.

    Raises :class:`SpecError` when ``binding.options`` is not a mapping — a malformed *binding*,
    not a malformed request. The message names the stream and never an option value, which may be
    connector configuration carrying a credential.
    """
    options = binding.options
    if not isinstance(options, dict):
        # Checked before the early return, so a malformed binding is refused on every request and
        # never reaches `build_body`, which reads `options["passthrough"]` the same way.
        raise SpecError(
            f"stream {binding.name!r} has malformed options "
            f"({type(options).__name__}, expected a mapping)"
        )
    named = (req.alias or "").strip()
    if not named or named == binding.alias:
        return binding.alias
    if options.get(ALIAS_OVERRIDE_OPTION) is True and named in KNOWN_ALIASES:
        return named
    return None


@dataclass(frozen=True)
class IngressResult(InferenceResult):
    """An :class:`InferenceResult` plus ``replayed`` (E18): the request's idempotency key had
    already been seen, so it was served but not counted in telemetry or drift — and
    ``shed_reason``: ``"in_flight"`` or ``"rate"`` when the ingress shed the request itself
    (its own backpressure, not the model service's answer), ``None`` for every other result."""

    replayed: bool = False
    shed_reason: str | None = None

    @classmethod
    def of(
        cls,
        result: InferenceResult,
        *,
        replayed: bool = False,
        shed_reason: str | None = None,
    ) -> IngressResult:
        base = {f.name: getattr(result, f.name) for f in fields(InferenceResult)}
        return cls(**base, replayed=replayed, shed_reason=shed_reason)


def _error(
    outcome: Outcome,
    detail: str | None = None,
    *,
    retry_after: float | None = None,
    shed_reason: str | None = None,
) -> IngressResult:
    body: dict[str, Any] = {"error": outcome}
    if detail:
        body["detail"] = detail[:_DETAIL_MAX]
    return IngressResult(
        outcome=outcome,
        body=body,
        retry_after=retry_after,
        status=CALLER_STATUS[outcome],
        shed_reason=shed_reason,
    )


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _embedding_of(payload: dict[str, Any]) -> list[float] | None:
    """The request's embedding (``payload["embedding"]`` or ``payload["features"]["embedding"]``,
    as the bridge reads it), or ``None`` if absent or not a list of numbers."""
    vec = payload.get("embedding")
    if vec is None:
        features = payload.get("features")
        if isinstance(features, dict):
            vec = features.get("embedding")
    if not isinstance(vec, list | tuple) or not vec:
        return None
    values: list[float] = []
    for v in vec:
        number = _as_float(v)
        if number is None:
            return None
        values.append(number)
    return values


class _StreamCounters:
    __slots__ = ("in_flight", "outcomes", "replayed", "shed", "telemetry_dropped")

    def __init__(self) -> None:
        self.in_flight = 0
        self.outcomes: dict[str, int] = {}
        self.shed: dict[str, int] = {"in_flight": 0, "rate": 0}
        self.replayed = 0
        self.telemetry_dropped = 0


class StreamIngress:
    """Admission, inference and post-reply bookkeeping for every stream request.

    ``client`` routes to the model service; ``spool`` takes telemetry; ``drift`` (optional)
    aggregates model failures; ``schema`` (optional) is the model schema registry passed to
    ``build_body`` — ``None`` uses the process-wide default; ``coord`` (optional) is the
    coordinator for rate limits and idempotency — ``None`` resolves ``get_coordinator()`` the first
    time one is needed. ``clock`` times requests; ``now`` stamps telemetry.
    """

    def __init__(
        self,
        client: InferenceClient,
        spool: TelemetrySpool,
        drift: DriftAggregator | None = None,
        schema: ModelSchemaRegistry | None = None,
        coord: Coordinator | None = None,
        *,
        clock: Callable[[], float] = time.perf_counter,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._spool = spool
        self._drift = drift
        self._schema = schema
        self._coord = coord
        self._clock = clock
        self._now = now
        self._lock = threading.Lock()
        self._semaphores: dict[str, tuple[int, threading.BoundedSemaphore]] = {}
        self._counters: dict[str, _StreamCounters] = {}
        self._meta: dict[str, dict[str, str]] = {}
        self._last_warning: dict[str, float] = {}

    # ── public ───────────────────────────────────────────────────────────────────────────────

    def handle(
        self,
        binding: StreamBinding,
        req: StreamRequest,
        *,
        reply: Callable[[IngressResult], None] | None = None,
    ) -> IngressResult:
        """Serve one request. Never raises (except whatever ``reply`` itself raises, which is
        re-raised after the post-reply bookkeeping has run)."""
        started = self._clock()
        key = f"{binding.project}/{binding.name}"
        model = binding.model  # authoritative (M1): never the request's
        alias: str | None = binding.alias
        self._register(key, binding)
        counted = False
        try:
            # Inside the try (re-review): `resolve_alias` reads `binding.options`, and a binding
            # whose options are not a mapping would otherwise raise straight out of `handle`,
            # breaking "never raises into the caller". The catalog always coerces options to a
            # dict, so this is defence in depth for a binding built by hand or by a pack connector.
            alias = resolve_alias(binding, req)  # authoritative (C1); None = it named another
            if alias is None:
                result, counted = (
                    _error("validation", f"this stream is bound to alias {binding.alias}"),
                    False,
                )
            else:
                result, counted = self._serve(binding, req, key, alias)
        except SpecError as exc:
            # A malformed binding (its `options` are not a mapping): a `validation` result naming
            # the stream, never the value — an option value can be a connector credential.
            alias = binding.alias
            result, counted = _error("validation", str(exc)), False
        except Exception as exc:  # noqa: BLE001 - the ingress never raises into its caller
            logger.warning(
                "dataplane stream %s: ingress failed unexpectedly (%s)", key, type(exc).__name__
            )
            result = _error("unexpected")
        self._count(key, result.outcome, replayed=result.replayed)
        metrics.observe_request(
            binding.project,
            binding.name,
            binding.connector,
            model,
            result.outcome,
            self._clock() - started,
        )
        try:
            if reply is not None:
                reply(result)
        finally:
            if counted and not result.replayed:
                try:
                    self._after_reply(binding, req, model, alias or binding.alias, result)
                except Exception:  # noqa: BLE001 - bookkeeping never reaches the caller
                    logger.warning("dataplane stream %s: post-reply bookkeeping failed", key)
        return result

    def refresh_schema(self) -> None:
        """Re-read the active pack's model schemas (review I2).

        Called by :class:`~examlops.dataplane.streams.supervisor.StreamSupervisor` after every
        successful pack sync, so a model added — or an ``inference.input_schema`` changed — while
        the process runs is picked up on the same cadence the stream catalog is. With an injected
        registry it refreshes that registry; with none, it drops the process-wide default so the
        next :func:`~examlops.dataplane.streams.schema.build_body` rebuilds it. Never raises: a
        pack that cannot be read leaves the previous schemas in place.
        """
        try:
            if self._schema is not None:
                self._schema.refresh()
            else:
                from examlops.dataplane.streams.schema import reset_default_registry

                reset_default_registry()
        except Exception as exc:  # noqa: BLE001 - the old schemas stay usable
            logger.warning(
                "dataplane stream ingress: refreshing the model schemas failed (%s)",
                type(exc).__name__,
            )

    def stats(self) -> dict[str, dict[str, Any]]:
        """Per-stream counters, keyed ``project/stream``."""
        with self._lock:
            return {
                key: {
                    **self._meta.get(key, {}),
                    "requests": sum(c.outcomes.values()),
                    "outcomes": dict(c.outcomes),
                    "in_flight": c.in_flight,
                    "shed": dict(c.shed),
                    "replayed": c.replayed,
                    "telemetry_dropped": c.telemetry_dropped,
                }
                for key, c in self._counters.items()
            }

    # ── the request path ─────────────────────────────────────────────────────────────────────

    def _serve(
        self, binding: StreamBinding, req: StreamRequest, key: str, alias: str
    ) -> tuple[IngressResult, bool]:
        """Returns ``(result, counted)`` — ``counted`` means an ``ok``/``model`` outcome from the
        model service, the only results that reach telemetry and drift."""
        if req.model and req.model != binding.model:
            return _error("validation", f"this stream is bound to model {binding.model}"), False
        limits = binding.limits
        try:
            validate(req.payload, limits.max_bytes)
            body = build_body(binding, req.payload, registry=self._schema)
        except SpecError as exc:
            return _error("validation", str(exc)), False
        except (TypeError, ValueError) as exc:  # e.g. a value json cannot encode
            return _error("validation", f"invalid payload ({type(exc).__name__})"), False

        semaphore = self._semaphore(key, limits.max_in_flight)
        if not semaphore.acquire(blocking=False):
            self._shed(binding, key, "in_flight")
            return (
                _error("overloaded", retry_after=IN_FLIGHT_RETRY_AFTER_S, shed_reason="in_flight"),
                False,
            )
        in_flight = False
        try:
            self._in_flight(binding, key, +1)
            in_flight = True
            if limits.rate_per_min > 0 and not self._rate_allows(binding):
                self._shed(binding, key, "rate")
                retry_after = min(_RATE_WINDOW_S, _RATE_WINDOW_S / limits.rate_per_min)
                return _error("overloaded", retry_after=retry_after, shed_reason="rate"), False
            routed = replace(
                req,
                model=binding.model,
                alias=alias,
                deadline_ms=effective_budget_ms(limits.deadline_ms, req.deadline_ms),
            )
            try:
                inferred = self._client.infer(routed, body)
            except Exception as exc:  # noqa: BLE001 - a client that raises is a client bug
                logger.warning(
                    "dataplane stream %s: inference client raised (%s)", key, type(exc).__name__
                )
                return _error("unexpected"), False
        finally:
            semaphore.release()
            if in_flight:
                self._in_flight(binding, key, -1)

        if inferred.outcome not in _COUNTED:
            return IngressResult.of(inferred), False
        # E18 after inference (M2), outside the permit: only a counted outcome consumes the key.
        replayed = bool(req.idempotency_key) and not self._first_seen(
            binding, str(req.idempotency_key)
        )
        return IngressResult.of(inferred, replayed=replayed), True

    # ── after the reply ──────────────────────────────────────────────────────────────────────

    def _after_reply(
        self,
        binding: StreamBinding,
        req: StreamRequest,
        model: str,
        alias: str,
        result: IngressResult,
    ) -> None:
        """Offer telemetry (``ok`` only), then feed drift. Never raises, never blocks on I/O."""
        if result.outcome == "ok":
            try:
                self._offer_telemetry(binding, req, model, alias, result)
            except Exception:  # noqa: BLE001 - telemetry must never take the caller down
                logger.warning("dataplane stream %s: telemetry offer failed", binding.name)
        if self._drift is None:
            return
        # ok → a success, unless it carried no prediction (bridge parity); model → a failure.
        failed = result.outcome == "model" or result.prediction is None
        self._drift.observe(model, failed, stream=binding.name, connector=binding.connector)

    def _offer_telemetry(
        self,
        binding: StreamBinding,
        req: StreamRequest,
        model: str,
        alias: str,
        result: IngressResult,
    ) -> None:
        embedding: EmbeddingStats | None = None
        vector = _embedding_of(req.payload)
        if vector is not None:
            embedding = EmbeddingStats.from_vector(vector)
            metrics.set_embedding(model, embedding.norm, embedding.mean, embedding.std)
        version = result.body.get("model_version") if isinstance(result.body, dict) else None
        job_id = req.metadata.get("job_id") or req.payload.get("job_id")
        # I4: the tenant is NOT read from the request — not from its metadata, not from its
        # payload. A stream's tenancy unit is the binding's project, which the record carries;
        # the connectors' own `metadata.pop("tenant")` is now defence in depth, not the rule.
        record = TelemetryRecord(
            event_id=uuid.uuid4().hex,
            ts=self._now(),
            project=binding.project,
            stream=binding.name,
            connector=binding.connector,
            model=model,
            alias=alias,
            model_version=str(version) if version not in (None, "") else None,
            outcome=result.outcome,
            prediction=_as_float(result.prediction),
            embedding=embedding,
            job_id=str(job_id) if job_id is not None else None,
            traceparent=req.traceparent,
        )
        if not self._spool.offer(record):
            metrics.telemetry_dropped(binding.project, binding.name)
            with self._lock:
                self._counters[f"{binding.project}/{binding.name}"].telemetry_dropped += 1

    # ── coordinator (fail open) ──────────────────────────────────────────────────────────────

    def _coordinator(self) -> Coordinator:
        if self._coord is None:
            from examlops.coordination import get_coordinator

            self._coord = get_coordinator()
        return self._coord

    def _rate_allows(self, binding: StreamBinding) -> bool:
        bucket = f"dataplane-stream:{binding.project}:{binding.name}"
        try:
            return bool(
                self._coordinator().allow(bucket, binding.limits.rate_per_min, _RATE_WINDOW_S)
            )
        except Exception as exc:  # noqa: BLE001 - an unanswered rate check admits
            self._warn_coordinator("rate check", exc)
            return True

    def _first_seen(self, binding: StreamBinding, idempotency_key: str) -> bool:
        # `stream`, not `push`: the Kafka path reaches this too (M11).
        key = f"dataplane:stream:{binding.project}:{binding.name}:{idempotency_key}"
        try:
            return bool(self._coordinator().first_seen(key, IDEMPOTENCY_TTL_S))
        except Exception as exc:  # noqa: BLE001 - an unanswered check counts as first-seen
            self._warn_coordinator("idempotency check", exc)
            return True

    def _warn_coordinator(self, what: str, exc: Exception) -> None:
        """At most one warning per :data:`_LOG_EVERY_S` per check (M6): a failing rate check never
        hides a failing idempotency check."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_warning.get(what, float("-inf")) < _LOG_EVERY_S:
                return
            self._last_warning[what] = now
        logger.warning(
            "dataplane stream ingress: coordinator %s failed (%s); failing open",
            what,
            type(exc).__name__,
        )

    # ── bookkeeping ──────────────────────────────────────────────────────────────────────────

    def _register(self, key: str, binding: StreamBinding) -> None:
        with self._lock:
            if key not in self._counters:
                self._counters[key] = _StreamCounters()
                self._meta[key] = {
                    "project": binding.project,
                    "stream": binding.name,
                    "connector": binding.connector,
                }

    def _semaphore(self, key: str, size: int) -> threading.BoundedSemaphore:
        """The stream's permit pool. A changed ``max_in_flight`` gets a fresh pool; permits held
        on the old one are released to it (the reference travels with the request)."""
        size = max(1, int(size))
        with self._lock:
            current = self._semaphores.get(key)
            if current is None or current[0] != size:
                current = (size, threading.BoundedSemaphore(size))
                self._semaphores[key] = current
            return current[1]

    def _in_flight(self, binding: StreamBinding, key: str, delta: int) -> None:
        """Count a permit taken (+1) or returned (-1). The gauge write is best-effort: a metrics
        failure must neither break the request nor unbalance the +1/-1 pair (M4)."""
        with self._lock:
            self._counters[key].in_flight += delta
        try:
            if delta > 0:
                metrics.in_flight_inc(binding.project, binding.name)
            else:
                metrics.in_flight_dec(binding.project, binding.name)
        except Exception as exc:  # noqa: BLE001 - metrics never break a request
            logger.debug(
                "dataplane stream %s: in-flight gauge failed (%s)", key, type(exc).__name__
            )

    def _shed(self, binding: StreamBinding, key: str, reason: str) -> None:
        with self._lock:
            shed = self._counters[key].shed
            shed[reason] = shed.get(reason, 0) + 1
        metrics.shed(binding.project, binding.name, reason)

    def _count(self, key: str, outcome: str, *, replayed: bool) -> None:
        with self._lock:
            counters = self._counters[key]
            counters.outcomes[outcome] = counters.outcomes.get(outcome, 0) + 1
            if replayed:
                counters.replayed += 1


__all__ = [
    "ALIAS_OVERRIDE_OPTION",
    "IDEMPOTENCY_TTL_S",
    "IN_FLIGHT_RETRY_AFTER_S",
    "KNOWN_ALIASES",
    "LOCAL_SHED_REASONS",
    "IngressResult",
    "StreamIngress",
    "is_local_shed",
    "resolve_alias",
]
