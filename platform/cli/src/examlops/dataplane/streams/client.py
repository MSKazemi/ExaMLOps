"""Inference client for the dataplane stream ingress (ADR 0130/0131, Plan 2, task A5).

:class:`InferenceClient` is the seam the ingress calls; :class:`RayPipelineClient` is the S1
implementation, posting to the inference pipeline's ``/infer-pipeline/infer`` exactly as the
Dataplane bus bridge does today. The OIP/v2 client that replaces it (B1) plugs into the same Protocol,
so S1 imports nothing of examlops-83's untracked ``oip_client``/``serving.budgets``.

Status mapping (E4 + controller ruling R6) lives in **one table**, :data:`OUTCOME_TABLE`. The
pipeline answers every failure as JSON; a 500 is always ``{"error": "inference_failed", "detail":
…, "cause": …}`` with ``cause`` one of the pipeline's ``INFERENCE_FAILURE_CAUSES``. Only a
``model`` cause (or no cause, from an older pipeline) is evidence about the model and becomes the
``model`` outcome that feeds drift; an unknown cause fails safe to ``transport`` so an unfamiliar
answer can never masquerade as model drift.

The time budget: the ingress folds the binding's ``limits.deadline_ms`` and the caller's own
budget into ``StreamRequest.deadline_ms`` (:func:`effective_budget_ms`) before calling
:meth:`InferenceClient.infer`, so the Protocol stays ``infer(req, body)``. The client sends that
budget as ``X-ExaMLOps-Budget-Ms`` (omitted when there is none) and derives its own HTTP timeout
from it, plus a grace so the pipeline's own ``504`` can still arrive.

Answering the caller (E4 + R18) is one table too, :data:`OUTCOME_ANSWER`: status, problem-type
slug, title and detail per outcome. :data:`CALLER_STATUS` is derived from its first column, so the
status stamped on a result and the status the push route answers with cannot drift apart; the one
deliberate exception is :data:`PUSH_SHED` (the ingress's own backpressure answers 429, ruling R18).

Security: the request body is never logged — only the model, the stream and the outcome.
"""

from __future__ import annotations

import calendar
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import cookiejar
from typing import Any, Protocol

import httpx

from examlops.dataplane.streams.types import InferenceResult, Outcome, StreamRequest

logger = logging.getLogger(__name__)

#: Base URL of the inference pipeline (``EXAMLOPS_DATAPLANE_INFERENCE_URL``).
INFERENCE_URL_ENV = "EXAMLOPS_DATAPLANE_INFERENCE_URL"
DEFAULT_INFERENCE_URL = "http://ray-serving:8001"
INFER_PATH = "/infer-pipeline/infer"
#: The relative time budget, in whole milliseconds (the same header ``serving.budgets`` reads).
BUDGET_HEADER = "X-ExaMLOps-Budget-Ms"

#: HTTP timeouts when a request carries no budget — the platform's defaults (bridge parity).
_DEFAULT_CONNECT_S = 5.0
_DEFAULT_READ_S = 10.0
_DEFAULT_WRITE_S = 10.0
_DEFAULT_POOL_S = 5.0
#: Extra time past the budget, so the pipeline's own deadline answer (it waits its remaining
#: budget plus 0.5 s) reaches us instead of our timeout firing first.
_BUDGET_GRACE_S = 1.0
#: Upper bound on the model-failure detail carried back to the caller.
_DETAIL_MAX = 256
#: Ceiling on any ``Retry-After`` we pass on (seconds). The value becomes the caller's own
#: ``Retry-After`` and, for Kafka, a partition pause — one malformed or hostile header must not
#: park a partition for a year (or overflow ``time.sleep``).
RETRY_AFTER_MAX_S = 300.0
#: The shared client's connection-pool ceiling (``EXAMLOPS_DATAPLANE_INFERENCE_MAX_CONNECTIONS``).
MAX_CONNECTIONS_ENV = "EXAMLOPS_DATAPLANE_INFERENCE_MAX_CONNECTIONS"
DEFAULT_MAX_CONNECTIONS = 100

#: ``cause`` for an ``inference_failed`` body that has none (an older pipeline).
NO_CAUSE = ""

#: The one status mapping table (E4 + R6), keyed ``(HTTP status, cause)``.
#:
#: ``cause`` is ``None`` for any answer that is not an ``inference_failed`` body, :data:`NO_CAUSE`
#: for an ``inference_failed`` body without a cause, else the body's ``cause``. Fallbacks, applied
#: by :func:`classify_response` and pinned by a test each: a 500 ``inference_failed`` with an
#: unknown cause → ``transport``; a 500 that is not ``inference_failed`` → ``transport`` (same fail
#: safe: never drift on an answer we cannot read); any other status → ``unexpected``; a network
#: error, a connect timeout or a pool timeout → ``transport``; a read/write timeout while a budget
#: governed it → ``deadline`` (without a budget → ``transport``).
#:
#: ``502`` is a gateway (Envoy, Ray's proxy) reporting its upstream gone — retryable, never drift
#: (R9.1). ``429`` is the upstream rate-limiting us: ``overloaded``, ``Retry-After`` kept (R9.1).
OUTCOME_TABLE: dict[tuple[int, str | None], Outcome] = {
    (200, None): "ok",
    (422, None): "validation",
    (404, None): "not_found",
    (429, None): "overloaded",
    (502, None): "transport",
    (503, None): "overloaded",
    (504, None): "deadline",
    (500, NO_CAUSE): "model",
    (500, "model"): "model",
    (500, "timeout"): "deadline",
    (500, "transport"): "transport",
    (500, "replica_lost"): "transport",
    (500, "pipeline"): "transport",
    (500, "protocol"): "unexpected",
}
UNKNOWN_CAUSE_OUTCOME: Outcome = "transport"
UNREADABLE_500_OUTCOME: Outcome = "transport"
OTHER_STATUS_OUTCOME: Outcome = "unexpected"
NETWORK_ERROR_OUTCOME: Outcome = "transport"

#: The ONE outcome → answer table (review I3). Every surface that turns an ingress result into an
#: HTTP answer reads it: the push route (``POST /streams/{name}/messages``) builds its RFC 9457
#: problem document from the four fields, and :data:`CALLER_STATUS` — the status stamped on every
#: :class:`~examlops.dataplane.streams.types.InferenceResult`, and so the status the dead-letter
#: replay route answers with — is its first column. Two tables used to disagree here: the same
#: ``unexpected`` outcome answered 502 on one route and 500 on the other.
#:
#: ``model`` keeps the pipeline's 500 (a genuine server-side failure, neither the caller's fault
#: nor an answer); an upstream we could not use is a 502; ``unexpected`` is **500** — it means the
#: platform surprised itself (a client bug, an answer nobody can read), which is our own server
#: error and not a statement about the upstream.
#:
#: The single ruled exception is the push route's own backpressure: a request the ingress shed
#: itself answers :data:`PUSH_SHED` (429) rather than this table's 503 (ruling R18). It is the
#: caller's own rate that is too high, not the service that is unavailable.
OUTCOME_ANSWER: dict[Outcome, tuple[int, str, str, str]] = {
    "ok": (200, "ok", "OK", "the message was served"),
    "validation": (
        422,
        "validation",
        "Invalid message",
        "the message is not valid for this stream",
    ),
    "not_found": (404, "not-found", "Not found", "the model this stream is bound to is not served"),
    "overloaded": (503, "overloaded", "Service unavailable", "the inference service is overloaded"),
    "deadline": (504, "deadline", "Gateway timeout", "the inference budget ran out"),
    "model": (500, "model-failed", "Model failed", "the model failed on this message"),
    "transport": (502, "transport", "Bad gateway", "the inference service could not be used"),
    "unexpected": (500, "unexpected", "Internal server error", "the message could not be served"),
}

#: The push answer for a request the ingress shed itself (``shed_reason`` in
#: :data:`~examlops.dataplane.streams.ingress.LOCAL_SHED_REASONS`) — ruling R18's one deliberate
#: departure from :data:`OUTCOME_ANSWER`.
PUSH_SHED: tuple[int, str, str, str] = (
    429,
    "shed",
    "Too many requests",
    "this stream is at its in-flight or rate limit",
)

#: The status the ingress answers its own caller with, per outcome — the first column of
#: :data:`OUTCOME_ANSWER`, derived so the two can never drift apart.
CALLER_STATUS: dict[Outcome, int] = {
    outcome: answer[0] for outcome, answer in OUTCOME_ANSWER.items()
}


class InferenceClient(Protocol):
    """Routes one request to the model service. Must never raise: every failure is a result."""

    def infer(self, req: StreamRequest, body: dict[str, Any]) -> InferenceResult: ...


def effective_budget_ms(limit_ms: int | None, caller_ms: int | None) -> int | None:
    """``min(limit_ms, caller_ms)`` ignoring ``None``s; ``None`` when both are ``None``."""
    present = [v for v in (limit_ms, caller_ms) if v is not None]
    return min(present) if present else None


_DELTA_SECONDS = re.compile(r"[0-9]+", re.ASCII)
#: IMF-fixdate (RFC 9110 §5.6.7), the only HTTP-date form a sender may generate:
#: ``Sun, 06 Nov 1994 08:49:37 GMT``. The obsolete RFC 850 / asctime forms are refused.
_IMF_FIXDATE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), ([0-9]{2}) "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) ([0-9]{4}) "
    r"([0-9]{2}):([0-9]{2}):([0-9]{2}) GMT",
    re.ASCII,
)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _clamp_retry_after(seconds: float) -> float:
    return min(RETRY_AFTER_MAX_S, max(0.0, seconds))


def parse_retry_after(value: str | None, *, now: Callable[[], float] = time.time) -> float | None:
    """``Retry-After`` as seconds from now, clamped to ``[0, RETRY_AFTER_MAX_S]``.

    Only the two RFC 9110 §10.2.3 forms are accepted: delta-seconds as plain ASCII digits
    (``1*DIGIT`` — so no sign, exponent, ``_`` or decimal point), or an IMF-fixdate. A date in
    the past is ``0.0``; anything else (garbage, an obsolete date form, an impossible date) is
    ``None``.
    """
    if value is None:
        return None
    text = value.strip()
    if _DELTA_SECONDS.fullmatch(text):
        # A digit string too long to be a sane delta is simply "a long time": clamp, without ever
        # asking int() to convert an arbitrarily long string.
        return RETRY_AFTER_MAX_S if len(text) > 9 else _clamp_retry_after(float(int(text)))
    match = _IMF_FIXDATE.fullmatch(text)
    if match is None:
        return None
    day, month, year, hour, minute, second = match.groups()
    fields = (int(year), _MONTHS.index(month) + 1, int(day), int(hour), int(minute), int(second))
    try:
        stamp = calendar.timegm((*fields, 0, 0, 0))
        # timegm() normalises out-of-range fields (day 32 → next month); refuse those instead.
        if tuple(time.gmtime(stamp)[:6]) != fields:
            return None
    except (OverflowError, ValueError):
        return None
    return _clamp_retry_after(stamp - now())


def _result(
    outcome: Outcome,
    *,
    body: dict[str, Any] | None = None,
    prediction: Any = None,
    retry_after: float | None = None,
) -> InferenceResult:
    return InferenceResult(
        outcome=outcome,
        prediction=prediction,
        body=body if body is not None else {"error": outcome},
        retry_after=retry_after,
        status=CALLER_STATUS[outcome],
    )


def classify_response(
    status: int,
    payload: Any,
    headers: httpx.Headers | dict[str, str] | None = None,
    *,
    now: Callable[[], float] = time.time,
) -> InferenceResult:
    """Map one pipeline answer (status + decoded JSON body, or ``None``) to an InferenceResult.

    Pure — the whole of the R6 table, testable without a transport. The upstream body is carried
    back only on success; a failure carries a small, payload-free error body (an upstream 422 in
    particular is never forwarded: a validation error body can echo the input).
    """
    if status == 200:
        if not isinstance(payload, dict):
            return _result(OTHER_STATUS_OUTCOME, body={"error": "unexpected", "detail": "no body"})
        return _result("ok", body=payload, prediction=payload.get("prediction"))

    if status == 500:
        body = payload if isinstance(payload, dict) else {}
        if body.get("error") != "inference_failed":
            return _result(UNREADABLE_500_OUTCOME, body={"error": "transport", "upstream": 500})
        raw_cause = body.get("cause")
        cause = NO_CAUSE if raw_cause is None else str(raw_cause)
        outcome = OUTCOME_TABLE.get((500, cause), UNKNOWN_CAUSE_OUTCOME)
        err: dict[str, Any] = {"error": outcome, "cause": cause or None, "upstream": 500}
        if outcome == "model":
            err["detail"] = str(body.get("detail") or "inference_failed")[:_DETAIL_MAX]
        return _result(outcome, body=err)

    outcome = OUTCOME_TABLE.get((status, None), OTHER_STATUS_OUTCOME)
    retry_after = None
    if outcome == "overloaded":
        retry_after = parse_retry_after(_header(headers, "retry-after"), now=now)
    return _result(outcome, body={"error": outcome, "upstream": status}, retry_after=retry_after)


def _header(headers: httpx.Headers | dict[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    if isinstance(headers, httpx.Headers):
        return headers.get(name)
    return next((v for k, v in headers.items() if k.lower() == name), None)


@dataclass(frozen=True)
class _Budget:
    ms: int | None

    def timeout(self, default: httpx.Timeout) -> httpx.Timeout:
        if self.ms is None:
            return default
        seconds = max(0.0, self.ms / 1000.0) + _BUDGET_GRACE_S
        return httpx.Timeout(
            connect=min(_DEFAULT_CONNECT_S, seconds),
            read=seconds,
            write=min(_DEFAULT_WRITE_S, seconds),
            pool=min(_DEFAULT_POOL_S, seconds),
        )


class _DenyAllCookies(cookiejar.CookiePolicy):
    """A cookie policy that refuses to store or send anything (review P2).

    One process-wide :class:`httpx.Client` serves every stream of every project. Its default
    cookie jar is shared state written by the *upstream*: a ``Set-Cookie`` on one tenant's answer
    would be stored once and then replayed on every later request, whoever it belongs to. The
    inference pipeline sets no cookies, so nothing is lost by never keeping one — and if a proxy
    in front of it ever starts to, it cannot leak across tenants.
    """

    netscape = True
    rfc2965 = False
    hide_cookie2 = True

    def set_ok(self, cookie: Any, request: Any) -> bool:
        return False

    def return_ok(self, cookie: Any, request: Any) -> bool:
        return False

    def domain_return_ok(self, domain: str, request: Any) -> bool:
        return False

    def path_return_ok(self, path: str, request: Any) -> bool:
        return False


def deny_all_cookies() -> cookiejar.CookieJar:
    """A fresh cookie jar that stores and sends nothing (see :class:`_DenyAllCookies`)."""
    return cookiejar.CookieJar(policy=_DenyAllCookies())


def _max_connections() -> int:
    raw = os.getenv(MAX_CONNECTIONS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_CONNECTIONS
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(
            "dataplane inference client: %s=%r is not an integer; using %d",
            MAX_CONNECTIONS_ENV,
            raw,
            DEFAULT_MAX_CONNECTIONS,
        )
        return DEFAULT_MAX_CONNECTIONS


class RayPipelineClient:
    """Sync client for the inference pipeline: ONE shared ``httpx.Client``.

    A sync ``httpx.Client`` is thread-safe and has no event loop to share, so every request
    thread (anyio's worker pool, the Kafka loop, the bus loop) uses the same client and its one
    bounded connection pool (``httpx.Limits(max_connections=…)``, default 100, env-overridable
    via ``EXAMLOPS_DATAPLANE_INFERENCE_MAX_CONNECTIONS``). A per-thread client would leak one pool
    per thread that ever called in (anyio retires idle workers after 10 s).

    ``base_url`` defaults to ``EXAMLOPS_DATAPLANE_INFERENCE_URL`` or :data:`DEFAULT_INFERENCE_URL`;
    ``timeout`` (seconds) is the read timeout for a request without a budget; ``max_connections``
    overrides the env/default pool ceiling; ``transport`` is for tests (``httpx.MockTransport``).
    After :meth:`close`, :meth:`infer` answers ``transport`` (a draining replica: retry elsewhere).
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        *,
        max_connections: int | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        url = base_url or os.getenv(INFERENCE_URL_ENV, "").strip() or DEFAULT_INFERENCE_URL
        self._base_url = url.rstrip("/")
        self._default_timeout = httpx.Timeout(
            connect=_DEFAULT_CONNECT_S,
            read=_DEFAULT_READ_S if timeout is None else timeout,
            write=_DEFAULT_WRITE_S,
            pool=_DEFAULT_POOL_S,
        )
        pool = max(1, max_connections) if max_connections is not None else _max_connections()
        self._limits = httpx.Limits(max_connections=pool, max_keepalive_connections=min(pool, 20))
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=self._default_timeout,
            limits=self._limits,
            transport=transport,
            # One client serves every tenant: never store or replay an upstream cookie (P2).
            cookies=deny_all_cookies(),
        )
        self._close_lock = threading.Lock()
        self._closed = False

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def limits(self) -> httpx.Limits:
        return self._limits

    @property
    def closed(self) -> bool:
        return self._closed

    def infer(self, req: StreamRequest, body: dict[str, Any]) -> InferenceResult:
        budget = _Budget(req.deadline_ms)
        headers: dict[str, str] = {}
        if budget.ms is not None:
            headers[BUDGET_HEADER] = str(max(0, int(budget.ms)))
        if req.traceparent:
            headers["traceparent"] = req.traceparent
        # Routing fields come from the request (the binding), and win over the payload's own.
        json_body = {**body, "model_name": req.model, "alias": req.alias}
        job_id = req.metadata.get("job_id")
        if job_id is not None:
            json_body["job_id"] = str(job_id)
        if self._closed:
            return _result(NETWORK_ERROR_OUTCOME, body={"error": "transport", "detail": "closed"})
        try:
            resp = self._http.post(
                INFER_PATH,
                json=json_body,
                headers=headers,
                timeout=budget.timeout(self._default_timeout),
            )
        except (httpx.ConnectTimeout, httpx.PoolTimeout):
            # Never reached the upstream (unreachable host / our own pool exhausted): retryable,
            # and nothing to do with the budget — these fire on their own short caps.
            logger.debug(
                "stream inference connect/pool timeout: stream=%s model=%s", req.stream, req.model
            )
            return _result(NETWORK_ERROR_OUTCOME, body={"error": "transport", "detail": "timeout"})
        except httpx.TimeoutException:
            # A read/write timeout: those are derived from the budget, so firing means it is spent.
            outcome: Outcome = "deadline" if budget.ms is not None else NETWORK_ERROR_OUTCOME
            logger.debug("stream inference timed out: stream=%s model=%s", req.stream, req.model)
            return _result(outcome, body={"error": outcome, "detail": "timeout"})
        except httpx.HTTPError as exc:
            logger.debug(
                "stream inference transport error: stream=%s model=%s error=%s",
                req.stream,
                req.model,
                type(exc).__name__,
            )
            return _result(NETWORK_ERROR_OUTCOME, body={"error": "transport"})
        except Exception as exc:  # noqa: BLE001 - the Protocol promises a result, never a raise
            if self._closed:  # closed under us mid-request: a draining replica, not a bug
                return _result(
                    NETWORK_ERROR_OUTCOME, body={"error": "transport", "detail": "closed"}
                )
            logger.warning(
                "stream inference failed unexpectedly: stream=%s model=%s error=%s",
                req.stream,
                req.model,
                type(exc).__name__,
            )
            return _result("unexpected", body={"error": "unexpected"})
        try:
            payload: Any = resp.json()
        except ValueError:
            payload = None
        return classify_response(resp.status_code, payload, resp.headers)

    def close(self) -> None:
        """Close the shared client and its connection pool. Idempotent.

        It does not abort a request already on the wire: ``httpx.Client.close()`` closes the pool
        and its idle connections, while a call already waiting for a response runs to its own
        timeout (P4 — this used to claim the in-flight request "fails with a transport error
        instead", which it does not). What ``close`` does guarantee is that every *later*
        :meth:`infer` answers ``transport`` at once, without opening a connection, so a draining
        replica sheds new work immediately and the drain's own bounded wait covers the rest.
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._http.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("closing the inference client failed", exc_info=True)


__all__ = [
    "BUDGET_HEADER",
    "CALLER_STATUS",
    "DEFAULT_INFERENCE_URL",
    "DEFAULT_MAX_CONNECTIONS",
    "INFERENCE_URL_ENV",
    "INFER_PATH",
    "MAX_CONNECTIONS_ENV",
    "NETWORK_ERROR_OUTCOME",
    "NO_CAUSE",
    "OTHER_STATUS_OUTCOME",
    "OUTCOME_ANSWER",
    "OUTCOME_TABLE",
    "PUSH_SHED",
    "RETRY_AFTER_MAX_S",
    "UNKNOWN_CAUSE_OUTCOME",
    "UNREADABLE_500_OUTCOME",
    "InferenceClient",
    "RayPipelineClient",
    "classify_response",
    "deny_all_cookies",
    "effective_budget_ms",
    "parse_retry_after",
]
