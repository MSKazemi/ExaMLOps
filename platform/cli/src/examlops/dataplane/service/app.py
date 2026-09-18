"""Dataplane HTTP service (ADR 0130 §9). Every route is a library call; no logic lives here.

Container port 8010 (host 18010, loopback-only on n1). Auth is in :mod:`.auth`: the static
``DATAPLANE_TOKEN`` and, when a trust file is configured, federated data-center tokens (ADR 0120).
Pulls run on the :class:`~.scheduler.Scheduler`'s worker pool through ``run_pull``; the scheduler
also runs each source on its ``schedule``.

**Streams (ADR 0131, Plan 2 task A8).** ``POST /streams/{name}/messages`` pushes one message to an
HTTP push stream (scope ``ingest``); ``GET /streams`` and ``GET /streams/{name}`` report the
catalog, the supervisor's view and the ingress counters (scope ``read``). The
:class:`~examlops.dataplane.streams.supervisor.StreamSupervisor` runs every connector-backed
stream. One :class:`~examlops.dataplane.streams.supervisor.IngressStack` per process serves both.

**Runtime control and dead letters (ADR 0131 d7, Plan 2 task A8b).** ``POST
/streams/{name}/state`` (scope ``write``) sets a binding's admin state (``enabled``/``paused``/
``disabled``), persisted through :func:`examlops.data.dataplane.set_stream_state` (a second
replica sees it on its next reconcile) and — for this process's own live run — applied at once
through :meth:`~examlops.dataplane.streams.supervisor.StreamSupervisor.pause_stream`/
``resume_stream``. A no-op transition (already in the requested state) answers ``changed: false``
and writes no audit row; a reason the pack sweep reserved for itself (``removed_from_pack``,
``removed_from_pack:paused``) is refused (422). The dead-letter routes — ``GET
/streams/{name}/dead-letters`` (list, scope ``read``, never the payload), ``GET
/streams/{name}/dead-letters/{id}`` (one, scope ``read``; the payload only with the ``write``
scope *and* ``include_payload=true``, audited), ``POST …/{id}/replay`` (scope ``write``, a
claim-lost outcome answers 409) and ``DELETE /streams/{name}/dead-letters?older_than=`` (scope
``write``, purge, the count) — are thin wrappers over
:mod:`examlops.dataplane.streams.dlq`. All of it is project-scoped exactly like the rest of the
stream surface (the fixed 403, no existence oracle), and **every** ``/streams`` route answers
errors as RFC 9457 problem documents (``application/problem+json`` with ``type``/``title``/
``status``/``detail``, plus ``outcome`` where there is one) — one error shape for the whole
family, and the push route's statuses come from one table shared with the replay route
(:data:`examlops.dataplane.streams.client.OUTCOME_ANSWER`).

**Roles (E11).** ``EXAMLOPS_DATAPLANE_ROLE`` = ``all`` (default) | ``api`` | ``streams``; anything
else fails at startup. ``all`` and ``api`` mount the source, pull and stream routes and run the
pull scheduler; ``all`` and ``streams`` run the stream supervisor. Every role serves ``/health``,
``/ready`` and ``/metrics``.

**Drain (E10).** One budget, ``EXAMLOPS_DATAPLANE_DRAIN_SECONDS`` (20), one deadline: ``t0 +
budget``, ``t0`` being SIGTERM or the lifespan exit, whichever comes first. From ``t0`` the
service (1) answers 503 on ``/ready`` and push (push with ``Retry-After: 5``); (2) stops the
supervisor's connectors, which commit — at SIGTERM itself, on a background thread; (3) lets
in-flight pushes finish (the server's own graceful-shutdown timeout is the same budget, see
``platform/services/dataplane/main.py``); (4) closes drift, then the telemetry spool (flush),
then the inference client; (5) releases the stream leader leases; (6) stops the scheduler. Every
wait uses only the time left; past the deadline only the two flushes get half a second each, so
the drain ends within the budget plus about a second. Keep the budget below the orchestrator's
stop grace period (Compose sets 30 s for ``dataplane``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import os
import re
import signal
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from examlops import dataplane
from examlops.data import dataplane as catalog
from examlops.dataplane.safety import redact
from examlops.dataplane.service.auth import (
    FORBIDDEN_DETAIL,
    STREAM_FORBIDDEN_DETAIL,
    Caller,
    auth_mode,
    authenticate_ingest,
    authenticate_read,
    authenticate_write,
    authorize_source,
    authorize_stream,
    may_access_project,
    require_read,
)
from examlops.dataplane.service.scheduler import Scheduler, parse_interval, parse_timestamp
from examlops.dataplane.types import DataplaneError, Limits, SpecError

logger = logging.getLogger(__name__)

#: E11 — which part of the service this process runs.
ROLE_ENV = "EXAMLOPS_DATAPLANE_ROLE"
ROLES = ("all", "api", "streams")
#: E3 — the push body cap in bytes (a stream's own ``limits.max_bytes`` may lower it).
PUSH_MAX_BYTES_ENV = "EXAMLOPS_DATAPLANE_PUSH_MAX_BYTES"
DEFAULT_PUSH_MAX_BYTES = 1_048_576
#: E10 — how long the drain waits for in-flight pushes (and bounds the whole drain).
DRAIN_SECONDS_ENV = "EXAMLOPS_DATAPLANE_DRAIN_SECONDS"
DEFAULT_DRAIN_SECONDS = 20.0
#: ``Retry-After`` (seconds) while draining, and for a paused stream.
DRAINING_RETRY_AFTER_S = 5
PAUSED_RETRY_AFTER_S = 30
#: ``Retry-After`` for an upstream overload that named none.
OVERLOADED_RETRY_AFTER_S = 5
#: The ``X-ExaMLOps-Budget-Ms`` a push may ask for is clamped into [1, this].
PUSH_BUDGET_MAX_MS = 60_000
IDEMPOTENCY_KEY_MAX = 200
#: RFC 9457 problem ``type`` prefix for push errors: an absolute URI that names, not locates.
PROBLEM_TYPE_BASE = "urn:examlops:problem:dataplane-stream/"
_PROBLEM_MEDIA_TYPE = "application/problem+json"
#: The whole stream surface answers RFC 9457 problem documents (review M9): the push route used
#: to, while ``POST /streams/{n}/state``, the dead-letter routes and the read routes answered
#: FastAPI's ``{"detail": …}`` — two error shapes on one resource family. ``detail`` is still
#: present in every body, so a caller reading that key sees no change.
_STREAM_PATH = re.compile(r"/streams(/.*)?")
_IDEMPOTENCY_KEY = re.compile(rf"[\x21-\x7e]{{1,{IDEMPOTENCY_KEY_MAX}}}")
_TRACEPARENT = re.compile(r"[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")
_BUDGET = re.compile(r"[+-]?[0-9]{1,18}")
#: M1 — how long a push may take to send its whole body before it is answered 408.
PUSH_READ_TIMEOUT_ENV = "EXAMLOPS_DATAPLANE_PUSH_READ_TIMEOUT"
DEFAULT_PUSH_READ_TIMEOUT_S = 10.0
#: Drain step 4: each of the two flushes (drift, spool) gets at least this long, even past the
#: deadline — together they are the drain's worst-case overrun (1 s).
_FLUSH_FLOOR_S = 0.5
#: How long a failed ingress-stack build is remembered before the next request retries it.
_STACK_RETRY_S = 5.0
_CONTENT_LENGTH = re.compile(r"[0-9]{1,18}")

_COMMITTED = ("succeeded", "unchanged")
_ACTIVE = ("running", "committing")
# How long /health waits for the snapshot store before reporting it down.
_STORE_PROBE_TIMEOUT_S = 3.0


class SourceBody(BaseModel):
    """``PUT /sources/{name}`` — the same options as ``exa dataplane sources create``.

    Unknown keys are refused: a credential typed at the top level must not be silently dropped
    (or, worse, accepted by a later version). Credentials live in Named Connections.
    """

    model_config = ConfigDict(extra="forbid")

    connector: str
    project: str = ""
    connection: str | None = None
    spec: dict[str, Any] = {}
    schedule: str | None = None
    limits: dict[str, Any] | None = None
    contract: str | None = None
    enabled: bool = True


class PullBody(BaseModel):
    """``POST /sources/{name}/pull`` — what ``exa dataplane pull --remote`` sends."""

    model_config = ConfigDict(extra="forbid")

    project: str = ""
    full: bool = False


#: ``reason`` on ``POST /streams/{name}/state``.
_STATE_REASON_MAX = 200


class StreamStateBody(BaseModel):
    """``POST /streams/{name}/state`` (ADR 0131 d7, Plan 2 task A8b) — sets a stream binding's
    admin state. ``reason`` is stored verbatim in ``state_reason``; it may not be one the
    pack-removal sweep reserves for itself
    (:func:`examlops.dataplane.streams.bindings.is_sweep_reason`), the single source of truth for
    which reasons are the sweep's own — a runtime state change through this route must never
    collide with them, or a later pack sync could mistake a human's pause/disable for its own
    sweep and silently "restore" it."""

    model_config = ConfigDict(extra="forbid")

    # A pydantic Literal cannot be built from a tuple at class-definition time, so this repeats
    # `examlops.data.dataplane.STREAM_STATES`. A guard test asserts the two agree (review M4).
    state: Literal["enabled", "paused", "disabled"]
    reason: str | None = Field(default=None, max_length=_STATE_REASON_MAX)


def _state_change_event(prior_state: str, new_state: str) -> str:
    """The audit action for a state transition ending at ``new_state``: reaching ``enabled`` from
    ``paused`` is a *resume*, not a plain *enable* — the same persisted value, two different
    operator intents (A8b)."""
    if new_state == "enabled" and prior_state == "paused":
        return "dataplane_stream_resumed"
    return f"dataplane_stream_{new_state}"


def _source(s: Any) -> dict[str, Any]:
    return {
        "project": s.project,
        "name": s.name,
        "connector": s.connector,
        "connection": s.connection,
        "schedule": s.schedule,
        "enabled": s.enabled,
        "spec": s.spec,
        "limits": s.limits.to_dict(),
        "contract": s.contract,
    }


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, redact(str(exc)))


def _existing(name: str, project: str) -> Any:
    try:
        return dataplane.get_source_def(name, project)
    except SpecError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, redact(str(exc))) from None


def _actor(caller: Caller | None) -> str | None:
    return caller.actor if caller is not None else None


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """422 with only ``loc``/``type``/``msg`` per error.

    FastAPI's default body echoes each rejected ``input`` (plus ``ctx``/``url``) — a credential
    typed into a refused field (``{"token": "…"}``) would come straight back in the response.
    """
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    detail = [
        {
            "loc": [str(part) for part in e.get("loc", ())],
            "type": str(e.get("type", "")),
            "msg": redact(str(e.get("msg", ""))),
        }
        for e in errors
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": detail}
    )


class _StoreProbe:
    """At most one store probe in flight, and a bounded wait for it, so ``/health`` cannot hang
    on an unreachable object store (botocore retries can take minutes)."""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._thread: threading.Thread | None = None
        self._box: dict[str, bool] = {}

    @staticmethod
    def _probe(box: dict[str, bool]) -> None:
        try:
            s = dataplane.store_from_env()
            # The bucket (the root's first segment) must exist. A missing prefix under it is only a
            # fresh store, but a missing bucket fails every publish — and `exists(root)` alone
            # reads both as False, so this used to report a store that cannot be written as OK.
            # (For a local-path store the first segment is a top-level directory: a no-op check.)
            stripped = s.root.lstrip("/")
            bucket = s.root[: len(s.root) - len(stripped)] + stripped.split("/", 1)[0]
            if not s.fs.exists(bucket):
                box["ok"] = False
                return
            if s.fs.exists(s.root):
                s.fs.ls(s.root)
            box["ok"] = True
        except Exception:  # noqa: BLE001 — reported as a component state, never raised
            box["ok"] = False

    def check(self, timeout: float) -> bool:
        with self._mu:
            if self._thread is None or not self._thread.is_alive():
                self._box = {}
                self._thread = threading.Thread(
                    target=self._probe, args=(self._box,), name="dataplane-store-probe", daemon=True
                )
                self._thread.start()
            # else: the previous probe is still hanging — wait on it rather than pile up another
            thread, box = self._thread, self._box
        thread.join(timeout)
        return not thread.is_alive() and box.get("ok", False)


def _env_number(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number") from None


def _freshness_collector() -> Any:
    """Per-source gauges computed at scrape time from the catalog (never cached, never stale)."""
    from prometheus_client.core import GaugeMetricFamily, Metric
    from prometheus_client.registry import Collector

    class _Collector(Collector):
        def collect(self) -> Iterable[Metric]:
            fresh = GaugeMetricFamily(
                "dataplane_source_freshness_seconds",
                # last_pull(committed_only=True) counts `unchanged` pulls, and they stamp
                # finished_at: this is "last successful pull", not "last new snapshot".
                "Seconds since the source's last successful pull (succeeded or unchanged)",
                labels=["source"],
            )
            up = GaugeMetricFamily(
                "dataplane_source_up",
                "1 if the source's last finished pull committed (succeeded or unchanged); 0 if "
                "it failed, or if the pull catalog could not be read for this source",
                labels=["source"],
            )
            # Always emitted, with or without registered sources: `dataplane_source_up` only ever
            # has a series per *registered* source, so it cannot say anything about a catalog that
            # cannot be read at all (an install with zero sources would otherwise look identical
            # to one whose catalog is down). `DataplaneCatalogUnavailable` alerts on this instead.
            catalog_up = GaugeMetricFamily(
                "dataplane_catalog_up",
                "1 if the source catalog answered list_source_defs() this scrape, else 0",
            )
            # Final review I10: what `DataplaneSourceStale` compares freshness against (2 × this).
            # Only a source the scheduler actually runs gets a series — enabled, with a schedule
            # that parses — so an unscheduled or disabled source (pulled only on demand) never
            # reads as stale: the alert's `on(source)` join has nothing to match for it.
            schedule = GaugeMetricFamily(
                "dataplane_source_schedule_seconds",
                "The pull interval of an enabled, scheduled source, in seconds (from its schedule)",
                labels=["source"],
            )
            now = time.time()
            try:
                sources = dataplane.list_source_defs()
                catalog_up.add_metric([], 1.0)
            except Exception as exc:  # noqa: BLE001 — a scrape must not fail on the datastore
                logger.warning("dataplane metrics: catalog unavailable: %s", type(exc).__name__)
                sources = []
                catalog_up.add_metric([], 0.0)
            for src in sources:
                if src.enabled and src.schedule:
                    try:
                        schedule.add_metric([src.key], float(parse_interval(src.schedule)))
                    except SpecError:
                        pass  # the scheduler skips it too (and warns), so it is never pulled
                try:
                    recent = catalog.list_pulls(project=src.project, source=src.name, limit=10)
                    committed = catalog.last_pull(src.project, src.name)
                except Exception:  # noqa: BLE001
                    # A registered source always gets a series, even when its own read fails: it
                    # reads as a failing pull (0), not as an absent one — absence of the whole
                    # series means "not a registered source", which DataplanePullFailing relies on.
                    up.add_metric([src.key], 0.0)
                    continue
                # A pull in progress says nothing about health yet: judge the last finished one.
                finished = next((r for r in recent if r["status"] not in _ACTIVE), None)
                up.add_metric(
                    [src.key], 1.0 if finished and finished["status"] in _COMMITTED else 0.0
                )
                ts = parse_timestamp(committed.get("finished_at")) if committed else None
                if ts is not None:
                    fresh.add_metric([src.key], max(0.0, now - ts))
            yield fresh
            yield up
            yield catalog_up
            yield schedule

    return _Collector()


# Third-party HTTP clients log each request at INFO *with its full URL* — userinfo and signed query
# strings included (`HTTP Request: GET https://user:pw@host/x?sig=…`), which is exactly what the
# connectors' redaction exists to keep out of logs. Pinned to WARNING even if root is lowered.
_URL_LOGGING_LIBRARIES = ("httpx", "httpcore", "urllib3", "botocore", "s3fs", "fsspec", "aiohttp")


def configure_logging() -> None:
    """Container logging for the service: root at WARNING, ``examlops`` at INFO.

    Only this platform's own records (the startup auth-mode line, pull failures) are raised to
    INFO; every other library stays at WARNING, and the HTTP clients that log URLs are pinned
    there explicitly. Adds a stream handler only when root has none, so a host that configured
    logging keeps its handlers.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("examlops").setLevel(logging.INFO)
    for name in _URL_LOGGING_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)


# ── streams (ADR 0131, Plan 2 task A8) ──────────────────────────────────────────────────────


def drain_seconds() -> float:
    """``EXAMLOPS_DATAPLANE_DRAIN_SECONDS`` (default 20): the one shutdown budget — the server's
    graceful-shutdown timeout and the lifespan drain share it."""
    drain_s = _env_number(DRAIN_SECONDS_ENV, DEFAULT_DRAIN_SECONDS)
    if drain_s < 0:
        raise ValueError(f"{DRAIN_SECONDS_ENV} must be >= 0 seconds, not {drain_s}")
    return drain_s


def dataplane_role() -> str:
    """``EXAMLOPS_DATAPLANE_ROLE`` (default ``all``); anything else is a startup error."""
    raw = os.getenv(ROLE_ENV, "").strip().lower() or "all"
    if raw not in ROLES:
        raise ValueError(f"{ROLE_ENV}={raw!r} is not one of {', '.join(ROLES)}")
    return raw


class _InFlight:
    """Pushes that have entered the route and whose ingress call has not yet finished."""

    def __init__(self) -> None:
        self._n = 0
        self._cv = threading.Condition()

    def enter(self) -> None:
        with self._cv:
            self._n += 1

    def exit(self) -> None:
        with self._cv:
            self._n -= 1
            if self._n <= 0:
                self._cv.notify_all()

    @property
    def count(self) -> int:
        with self._cv:
            return self._n

    def wait_idle(self, timeout: float) -> bool:
        with self._cv:
            return self._cv.wait_for(lambda: self._n <= 0, timeout=max(0.0, timeout))


class _StreamRuntime:
    """The app's streaming state: the draining flag, in-flight pushes, the lazily built shared
    ingress stack, the supervisor (when this role runs one) and the catalog view."""

    def __init__(
        self,
        *,
        stack_factory: Callable[[], Any],
        catalog_view: Any,
        push_max_bytes: int,
        drain_s: float,
        push_read_timeout_s: float = DEFAULT_PUSH_READ_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.draining = threading.Event()
        self.inflight = _InFlight()
        self.catalog = catalog_view
        self.push_max_bytes = push_max_bytes
        self.push_read_timeout_s = push_read_timeout_s
        self.drain_s = drain_s
        self.supervisor: Any = None
        self._stack_factory = stack_factory
        self._stack: Any = None
        self._stack_failed_at: float | None = None
        self._clock = clock
        self._deadline: float | None = None
        self._lock = threading.Lock()
        self._intake_lock = threading.Lock()
        self._intake_started = False
        self._intake_done = threading.Event()

    def begin_drain(self) -> float:
        """Drain step 1: turn draining on and fix THE drain deadline — ``t0 + drain_s``, ``t0``
        being whichever comes first, SIGTERM or the lifespan exit. Later calls keep it."""
        with self._lock:
            if self._deadline is None:
                self._deadline = self._clock() + max(0.0, self.drain_s)
            deadline = self._deadline
        self.draining.set()
        return deadline

    def remaining(self) -> float:
        """Seconds left before the drain deadline (the whole budget if the drain has not begun)."""
        with self._lock:
            deadline = self._deadline
        if deadline is None:
            return max(0.0, self.drain_s)
        return max(0.0, deadline - self._clock())

    def stop_intake(self) -> None:
        """Drain step 2, once: the supervisor stops its connectors (they commit) within what is
        left of the budget. A second caller — the lifespan after SIGTERM started it — waits for
        the first to finish, bounded by the same deadline."""
        with self._intake_lock:
            first = not self._intake_started
            self._intake_started = True
        if not first:
            self._intake_done.wait(self.remaining())
            return
        try:
            supervisor = self.supervisor
            if supervisor is not None:
                supervisor.stop(self.remaining())
        finally:
            self._intake_done.set()

    def stack(self) -> Any:
        """The shared ingress stack, built on first use (never once the drain has begun). A build
        that fails is not retried for :data:`_STACK_RETRY_S` seconds, so a broken dependency is
        not rebuilt — and torn down again — on every request."""
        with self._lock:
            if self._stack is None:
                if self.draining.is_set():
                    raise RuntimeError("draining")
                failed_at = self._stack_failed_at
                if failed_at is not None and self._clock() - failed_at < _STACK_RETRY_S:
                    raise RuntimeError("the ingress stack failed to build moments ago")
                try:
                    self._stack = self._stack_factory()
                except Exception:
                    self._stack_failed_at = self._clock()
                    raise
                self._stack_failed_at = None
            return self._stack

    def built_stack(self) -> Any:
        with self._lock:
            return self._stack


def _problem(
    status_code: int,
    slug: str,
    title: str,
    detail: str,
    *,
    outcome: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """An RFC 9457 problem document. ``detail`` is always ours — never the caller's payload."""
    body: dict[str, Any] = {
        "type": PROBLEM_TYPE_BASE + slug,
        "title": title,
        "status": status_code,
        "detail": detail,
    }
    if outcome is not None:
        body["outcome"] = outcome
    return JSONResponse(
        body, status_code=status_code, headers=headers, media_type=_PROBLEM_MEDIA_TYPE
    )


_HTTP_PROBLEMS = {
    400: ("bad-request", "Bad request"),
    401: ("unauthorized", "Unauthorized"),
    403: ("forbidden", "Forbidden"),
    404: ("not-found", "Not found"),
    408: ("timeout", "Request timeout"),
    409: ("conflict", "Conflict"),
    413: ("too-large", "Content too large"),
    422: ("validation", "Invalid message"),
    500: ("unexpected", "Internal server error"),
    503: ("unavailable", "Service unavailable"),
}


async def _http_error(request: Request, exc: Exception) -> Response:
    """Every stream route's own errors (auth, authorisation, 404, 409) as RFC 9457 problem
    documents (M9); every other route keeps FastAPI's ``{"detail": …}`` exactly."""
    assert isinstance(exc, StarletteHTTPException)
    if _STREAM_PATH.fullmatch(request.url.path):
        slug, title = _HTTP_PROBLEMS.get(exc.status_code, ("error", "Error"))
        detail = exc.detail if isinstance(exc.detail, str) else title
        headers = dict(exc.headers) if exc.headers else None
        return _problem(exc.status_code, slug, title, detail, headers=headers)
    return await http_exception_handler(request, exc)


def _retry_after(seconds: float | None, default: int) -> str:
    if seconds is None or not math.isfinite(seconds):
        return str(default)
    return str(max(1, math.ceil(seconds)))


def _json_safe(value: Any) -> Any:
    """``value`` with every non-finite float as ``None``: the upstream's JSON decoder accepts
    ``NaN``, but a strict JSON answer cannot carry it."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return value


def _push_response(result: Any, binding: Any) -> Response:
    """Map one ingress result to the push answer, from the ONE table (review I3):
    :data:`examlops.dataplane.streams.client.OUTCOME_ANSWER`, plus ruling R18's single exception
    :data:`~examlops.dataplane.streams.client.PUSH_SHED` for the ingress's own backpressure. The
    dead-letter replay route answers ``result.status``, which is that table's first column, so the
    two routes can no longer answer differently for one outcome."""
    from examlops.dataplane.streams.client import OUTCOME_ANSWER, PUSH_SHED
    from examlops.dataplane.streams.ingress import LOCAL_SHED_REASONS

    outcome = result.outcome
    headers = {"Idempotent-Replayed": "true"} if getattr(result, "replayed", False) else {}
    if outcome == "ok":
        body = result.body if isinstance(result.body, dict) else {}
        return JSONResponse(
            {
                "ok": True,
                "prediction": _json_safe(result.prediction),
                "model": binding.model,
                "version": _json_safe(body.get("model_version")),
                "outcome": "ok",
            },
            headers=headers or None,
        )
    status_code, slug, title, detail = OUTCOME_ANSWER.get(outcome, OUTCOME_ANSWER["unexpected"])
    if outcome == "validation":
        raw = result.body.get("detail") if isinstance(result.body, dict) else None
        if raw:  # the ingress's own detail names the field, never the payload
            detail = redact(str(raw))[:300]
        return _problem(status_code, slug, title, detail, outcome=outcome)
    if outcome == "overloaded":
        if getattr(result, "shed_reason", None) in LOCAL_SHED_REASONS:
            # R18: our own backpressure is 429 (the caller's rate), not the table's 503.
            status_code, slug, title, detail = PUSH_SHED
            headers["Retry-After"] = _retry_after(result.retry_after, 1)
        else:
            headers["Retry-After"] = _retry_after(result.retry_after, OVERLOADED_RETRY_AFTER_S)
        return _problem(status_code, slug, title, detail, outcome=outcome, headers=headers)
    return _problem(status_code, slug, title, detail, outcome=outcome, headers=headers or None)


def _stream_project(project: str | None) -> str:
    """Streams spell the unscoped project ``""``; ``_global`` is accepted as its display name —
    :func:`examlops.dataplane.streams.types.normalize_project`, the one implementation (M3)."""
    from examlops.dataplane.streams.types import normalize_project

    return normalize_project(project)


def _ingress_stats(runtime: _StreamRuntime) -> dict[str, Any]:
    """Every stream's ingress counters, read **once** (review M10: ``stats()`` materialises a dict
    for every stream, and ``GET /streams`` called it once per binding — O(n²) in the stream
    count). ``{}`` when there is no stack, or the read failed: counters are informational."""
    stack = runtime.built_stack()
    if stack is None:
        return {}
    try:
        return dict(stack.ingress.stats())
    except Exception:  # noqa: BLE001 - counters are informational
        return {}


def _stream_view(
    binding: Any, runtime: _StreamRuntime, stats_by_stream: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A stream for the read routes: its definition (option *keys* only, never their values), the
    supervisor's view of it in this process and this process's ingress counters for it.
    ``stats_by_stream``, when given, is a snapshot :func:`_ingress_stats` already read."""
    supervisor = runtime.supervisor
    stats_all = _ingress_stats(runtime) if stats_by_stream is None else stats_by_stream
    stats = stats_all.get(f"{binding.project}/{binding.name}")
    runtime_status = None
    if supervisor is not None:
        try:
            runtime_status = supervisor.status_of(binding.project, binding.name)
        except Exception:  # noqa: BLE001
            runtime_status = None
    return {
        "project": binding.project,
        "name": binding.name,
        "connector": binding.connector,
        "model": binding.model,
        "alias": binding.alias,
        "address": redact(binding.address or ""),
        "connection": binding.connection,
        "state": binding.state,
        "origin": binding.origin,
        "limits": dataclasses.asdict(binding.limits),
        "option_keys": sorted(str(k) for k in (binding.options or {})),
        "runtime": runtime_status,
        "stats": stats,
    }


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """The request body, or ``None`` as soon as it passes ``limit`` bytes — whatever the headers
    said, so a chunked body without a Content-Length cannot get past the cap."""
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


#: ``GET /streams/{name}/dead-letters`` pagination (A8b): the default and the ceiling ``limit`` is
#: clamped to, and how many rows are read from the store before the route's own filter/cursor and
#: page-size slicing apply (matches ``dlq.list_dead_letters``'s own documented 1…1000 clamp).
_DLQ_LIST_DEFAULT = 50
_DLQ_LIST_MAX = 200
_DLQ_FETCH_CAP = 1000

_ISO_DURATION = re.compile(
    r"P(?:(?P<weeks>\d+(?:\.\d+)?)W)?(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?",
    re.IGNORECASE,
)


def _clamp_dlq_limit(limit: int) -> int:
    return max(1, min(int(limit), _DLQ_LIST_MAX))


def _parse_older_than(value: str) -> timedelta:
    """``older_than`` on ``DELETE /streams/{name}/dead-letters``: an ISO-8601 duration (``P7D``,
    ``PT1H``, ``P1DT12H``) or a plain, non-negative number of days."""
    v = value.strip()
    if not v:
        raise ValueError(
            "older_than is required (an ISO-8601 duration, e.g. P7D, or a number of days)"
        )
    m = _ISO_DURATION.fullmatch(v)
    if m and any(m.groups()):
        parts = {k: float(g) for k, g in m.groupdict().items() if g}
        return timedelta(
            weeks=parts.get("weeks", 0.0),
            days=parts.get("days", 0.0),
            hours=parts.get("hours", 0.0),
            minutes=parts.get("minutes", 0.0),
            seconds=parts.get("seconds", 0.0),
        )
    try:
        days = float(v)
    except ValueError:
        raise ValueError(
            f"older_than {value!r} must be an ISO-8601 duration (e.g. P7D) or a number of days"
        ) from None
    if days < 0:
        raise ValueError("older_than must not be negative")
    return timedelta(days=days)


def _replay_response(result: Any, binding: Any) -> JSONResponse:
    """One dead-letter replay's outcome as JSON, at ``result.status`` (already the right HTTP
    status per outcome — :data:`examlops.dataplane.streams.client.CALLER_STATUS`, and 409 for a
    lost claim: :func:`examlops.dataplane.streams.dlq.claim_lost_result`). Never the payload."""
    from examlops.dataplane.streams.dlq import REPLAY_DONE_OUTCOMES

    body = result.body if isinstance(result.body, dict) else {}
    payload: dict[str, Any] = {
        "outcome": result.outcome,
        "replayed": result.outcome in REPLAY_DONE_OUTCOMES,
        "model": binding.model,
    }
    if result.outcome == "ok":
        payload["prediction"] = _json_safe(result.prediction)
    detail = body.get("detail") or body.get("error")
    if detail:
        payload["detail"] = redact(str(detail))[:300]
    return JSONResponse(payload, status_code=result.status)


def _mount_stream_routes(router: APIRouter, rt: _StreamRuntime) -> None:
    from examlops.dataplane.streams.client import BUDGET_HEADER
    from examlops.dataplane.streams.supervisor import PUSH_CONNECTOR, CatalogUnavailable

    def _too_large(limit: int) -> JSONResponse:
        return _problem(
            413,
            "too-large",
            "Content too large",
            f"the message body is larger than {limit} bytes",
            outcome="validation",
        )

    @router.post("/streams/{name}/messages")
    async def push_message(
        name: str,
        request: Request,
        project: str = "",
        caller: Caller = Depends(authenticate_ingest),
    ) -> Response:
        """Push one message to an HTTP push stream (ADR 0131 d3, E2/E3/E4/E18) — synchronous: the
        status is the result, and the caller retries (with ``Idempotency-Key``).

        The body is ``{"payload": {...}, "model"?, "alias"?, "metadata"?}`` or a bare JSON object
        (the Kafka connector's envelope rule). Every failure is an RFC 9457 problem document with
        the ingress ``outcome`` as an extension; none ever echoes the payload.

        ``model`` and ``alias`` are checked against the binding, never obeyed: the binding is the
        authorization unit, so naming a different model — or a different alias, which would choose
        both the model *version* that answers and the drift window the prediction lands in — is a
        422. A stream whose ``options["allow_alias_override"]`` is ``true`` accepts a
        caller-supplied ``alias``, and then only one of the platform's known alias names
        (Production, Canary, Staging). ``metadata.tenant`` is always dropped.

        ====================================  ======================================
        result                                answer
        ====================================  ======================================
        ok                                    200 ``{"ok", "prediction", "model",
                                              "version", "outcome"}``
        validation (schema, not JSON)         422
        body over the cap                     413
        stream unknown/disabled/not push,     404 (only to a caller authorised for
        or model/alias not served             the project; anyone else gets 403)
        stream paused                         503 + ``Retry-After: 30``
        service draining                      503 + ``Retry-After: 5``
        local shed (in-flight/rate limit)     429 + ``Retry-After``
        upstream overloaded                   503 + ``Retry-After``
        deadline                              504
        transport                             502
        model                                 500, type ``…/model-failed``
        unexpected                            500
        ====================================  ======================================

        ``model`` is a 500 because the model itself failed on this input — a server-side failure,
        neither the caller's fault (not 422) nor an answer (not 200 ``ok: false``). A replayed
        ``Idempotency-Key`` answers ``Idempotent-Replayed: true`` and is not counted again.
        """
        rt.inflight.enter()
        handed_off = False
        try:
            # Counted before the check, so a drain that begins now waits for this request.
            if rt.draining.is_set():
                return _problem(
                    503,
                    "draining",
                    "Service unavailable",
                    "the dataplane service is draining",
                    headers={"Retry-After": str(DRAINING_RETRY_AFTER_S)},
                )
            proj = _stream_project(project)
            # Before the lookup: an unauthorised caller learns nothing, not even existence.
            await run_in_threadpool(authorize_stream, caller, request, "ingest", proj, name)
            declared: int | None = None
            raw_length = request.headers.get("content-length")
            if raw_length is not None:
                if not _CONTENT_LENGTH.fullmatch(raw_length.strip()):  # digits only: no sign
                    return _problem(400, "bad-request", "Bad request", "invalid Content-Length")
                declared = int(raw_length.strip())
                if declared > rt.push_max_bytes:
                    return _too_large(rt.push_max_bytes)
            try:
                binding = await run_in_threadpool(rt.catalog.get, proj, name)
            except CatalogUnavailable:
                return _problem(
                    503,
                    "unavailable",
                    "Service unavailable",
                    "the stream catalog cannot be read",
                    headers={"Retry-After": str(DRAINING_RETRY_AFTER_S)},
                )
            if binding is None or binding.state == "disabled":
                return _problem(
                    404, "not-found", "Not found", f"stream {name!r} not found", outcome="not_found"
                )
            if binding.connector != PUSH_CONNECTOR:
                return _problem(
                    404,
                    "not-push",
                    "Not found",
                    f"stream {name!r} does not accept HTTP push (connector {binding.connector})",
                    outcome="not_found",
                )
            if binding.state == "paused":
                return _problem(
                    503,
                    "paused",
                    "Service unavailable",
                    "stream paused",
                    headers={"Retry-After": str(PAUSED_RETRY_AFTER_S)},
                )
            limit = max(1, min(rt.push_max_bytes, int(binding.limits.max_bytes)))
            if declared is not None and declared > limit:
                return _too_large(limit)

            key = request.headers.get("idempotency-key")
            if key is not None and not _IDEMPOTENCY_KEY.fullmatch(key):
                return _problem(
                    422,
                    "validation",
                    "Invalid message",
                    f"Idempotency-Key must be 1-{IDEMPOTENCY_KEY_MAX} visible ASCII characters",
                    outcome="validation",
                )
            trace = (request.headers.get("traceparent") or "").strip()
            traceparent = trace if _TRACEPARENT.fullmatch(trace) else None  # malformed: ignored
            budget_ms: int | None = None
            raw_budget = request.headers.get(BUDGET_HEADER)
            if raw_budget is not None:
                if not _BUDGET.fullmatch(raw_budget.strip()):
                    return _problem(
                        422,
                        "validation",
                        "Invalid message",
                        f"{BUDGET_HEADER} must be an integer number of milliseconds",
                        outcome="validation",
                    )
                budget_ms = min(PUSH_BUDGET_MAX_MS, max(1, int(raw_budget.strip())))

            try:
                raw = await asyncio.wait_for(_read_capped(request, limit), rt.push_read_timeout_s)
            except TimeoutError:
                return _problem(
                    408,
                    "timeout",
                    "Request timeout",
                    f"the message body did not arrive within {rt.push_read_timeout_s:g}s",
                    outcome="validation",
                )
            except ClientDisconnect:  # the caller left mid-body: nobody to answer, nothing to log
                return Response(status_code=400)
            if raw is None:
                return _too_large(limit)
            from examlops.dataplane.streams.kafka_stream import EnvelopeRejected, parse_value
            from examlops.dataplane.streams.types import StreamRequest

            try:
                payload, model, alias, metadata = parse_value(raw, max_bytes=limit)
            except EnvelopeRejected as reject:
                if reject.reason == "oversize":
                    return _too_large(limit)
                return _problem(
                    422, "validation", "Invalid message", reject.detail, outcome="validation"
                )
            metadata.pop("tenant", None)  # defence in depth; the ingress never reads it (I4)
            req = StreamRequest(
                stream=binding.name,
                model=model,
                alias=alias,
                payload=payload,
                metadata=metadata,
                idempotency_key=key,
                traceparent=traceparent,
                deadline_ms=budget_ms,
            )
            try:
                stack = await run_in_threadpool(rt.stack)
            except Exception as exc:  # noqa: BLE001 - no stack: this replica cannot serve
                logger.warning("dataplane: stream ingress unavailable (%s)", type(exc).__name__)
                return _problem(
                    503,
                    "unavailable",
                    "Service unavailable",
                    "the stream ingress is unavailable",
                    headers={"Retry-After": str(DRAINING_RETRY_AFTER_S)},
                )

            # Reply first (A5 M3): the answer goes out when the ingress calls `reply`, while its
            # post-reply bookkeeping (telemetry offer, drift) finishes on the worker thread. The
            # in-flight count is released only once that thread is done.
            loop = asyncio.get_running_loop()
            replied: asyncio.Future[Any] = loop.create_future()

            def _deliver(res: Any) -> None:
                if not replied.done():
                    replied.set_result(res)

            def _reply(res: Any) -> None:
                try:
                    loop.call_soon_threadsafe(_deliver, res)
                except RuntimeError:  # the loop is gone (shutdown): nobody left to answer
                    pass

            def _finished(task: asyncio.Future[Any]) -> None:
                rt.inflight.exit()
                if not task.cancelled():
                    task.exception()  # retrieved: never "exception was never retrieved"

            task = asyncio.ensure_future(
                run_in_threadpool(stack.ingress.handle, binding, req, reply=_reply)
            )
            task.add_done_callback(_finished)
            handed_off = True
            await asyncio.wait({replied, task}, return_when=asyncio.FIRST_COMPLETED)
            if replied.done():
                result = replied.result()
            elif task.exception() is None:
                result = task.result()
            else:
                logger.warning("dataplane: stream ingress raised (%s)", type(task.exception()))
                return _problem(
                    500,
                    "unexpected",
                    "Internal server error",
                    "the message could not be served",
                    outcome="unexpected",
                )
            return _push_response(result, binding)
        finally:
            if not handed_off:
                rt.inflight.exit()

    @router.get("/streams")
    def streams(
        project: str | None = None, caller: Caller | None = Depends(require_read)
    ) -> list[dict[str, Any]]:
        from examlops.dataplane.streams.bindings import list_bindings

        scoped = None if project is None else _stream_project(project)
        if scoped is not None and not may_access_project(caller, "read", scoped):
            raise HTTPException(status.HTTP_403_FORBIDDEN, STREAM_FORBIDDEN_DETAIL)
        visible: dict[str, bool] = {}

        def _can_see(p: str) -> bool:
            if p not in visible:
                visible[p] = may_access_project(caller, "read", p, audit=False)
            return visible[p]

        stats_by_stream = _ingress_stats(rt)  # one read for the whole page (M10)
        return [
            _stream_view(b, rt, stats_by_stream)
            for b in list_bindings(scoped)
            if _can_see(b.project)
        ]

    @router.get("/streams/{name}")
    def stream(
        name: str,
        request: Request,
        project: str = "",
        caller: Caller | None = Depends(authenticate_read),
    ) -> dict[str, Any]:
        from examlops.dataplane.streams.bindings import get_binding

        proj = _stream_project(project)
        authorize_stream(caller, request, "read", proj, name)
        binding = get_binding(name, proj)
        if binding is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"stream {name!r} not found")
        return _stream_view(binding, rt)

    # ── runtime control and dead letters (ADR 0131 d7, Plan 2 task A8b) ─────────────────────

    @router.post("/streams/{name}/state")
    def set_stream_state_route(
        name: str,
        body: StreamStateBody,
        request: Request,
        project: str = "",
        caller: Caller = Depends(authenticate_write),
    ) -> dict[str, Any]:
        """Set a stream binding's admin state — see the module docstring. Conditional on the
        state read just before the write (``only_if_state``), so a caller acting on a stale read
        can never clobber a state change made since; a race that beats it leaves the row exactly
        as the winner left it, reported here as ``changed: false``."""
        from examlops.data.audit import write_audit_event
        from examlops.data.dataplane import get_stream, set_stream_state
        from examlops.dataplane.streams.bindings import is_sweep_reason

        proj = _stream_project(project)
        authorize_stream(caller, request, "write", proj, name)
        if is_sweep_reason(body.reason):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"reason {body.reason!r} is reserved for the pack-removal sweep",
            )
        current = get_stream(name, proj)
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"stream {name!r} not found")
        prior_state, new_state = current["state"], body.state
        if prior_state == new_state:
            return {"project": proj, "name": name, "state": new_state, "changed": False}
        changed = set_stream_state(
            proj, name, new_state, only_if_state=prior_state, reason=body.reason
        )
        if changed:
            details: dict[str, Any] = {
                "project": proj,
                "name": name,
                "previous_state": prior_state,
                "state": new_state,
            }
            if body.reason:
                details["reason"] = body.reason
            write_audit_event(
                "dataplane",
                caller.actor,
                _state_change_event(prior_state, new_state),
                f"{proj or '_global'}/{name}",
                details,
            )
            supervisor = rt.supervisor
            if supervisor is not None:
                if new_state == "paused":
                    supervisor.pause_stream(proj, name)
                elif new_state == "enabled":
                    supervisor.resume_stream(proj, name)
        return {"project": proj, "name": name, "state": new_state, "changed": changed}

    @router.get("/streams/{name}/dead-letters")
    def list_dead_letters_route(
        name: str,
        request: Request,
        project: str = "",
        limit: int = _DLQ_LIST_DEFAULT,
        cursor: str | None = None,
        reason: str | None = None,
        caller: Caller | None = Depends(authenticate_read),
    ) -> dict[str, Any]:
        """Metadata only (never a payload — ``has_payload`` says whether one is stored). ``limit``
        is clamped to 1…200; ``cursor`` is the ``id`` of the last item on the previous page."""
        from examlops.data.dataplane import get_stream
        from examlops.dataplane.streams.dlq import list_dead_letters

        proj = _stream_project(project)
        authorize_stream(caller, request, "read", proj, name)
        if get_stream(name, proj) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"stream {name!r} not found")
        rows = list_dead_letters(proj, stream=name, limit=_DLQ_FETCH_CAP)
        if reason is not None:
            rows = [r for r in rows if r.get("reason") == reason]
        if cursor:
            try:
                cursor_id = int(cursor)
            except ValueError:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "cursor must be an integer id"
                ) from None
            rows = [r for r in rows if int(r["id"]) < cursor_id]
        page_size = _clamp_dlq_limit(limit)
        page = rows[:page_size]
        next_cursor = (
            str(page[-1]["id"]) if len(page) == page_size and len(rows) > page_size else None
        )
        return {"items": page, "next_cursor": next_cursor}

    @router.get("/streams/{name}/dead-letters/{dead_letter_id}")
    def get_dead_letter_route(
        name: str,
        dead_letter_id: int,
        request: Request,
        project: str = "",
        include_payload: bool = False,
        caller: Caller | None = Depends(authenticate_read),
    ) -> dict[str, Any]:
        """The payload is included only when the caller holds the ``write`` scope *and*
        ``include_payload=true`` is set; that combination is audited
        (``dataplane_stream_dlq_payload_read``). Otherwise the ``payload`` key is simply absent —
        ``has_payload`` still says whether one is stored."""
        from examlops.data.audit import write_audit_event
        from examlops.data.dataplane import get_stream
        from examlops.dataplane.streams.dlq import get_dead_letter

        proj = _stream_project(project)
        authorize_stream(caller, request, "read", proj, name)
        if get_stream(name, proj) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"stream {name!r} not found")
        row = get_dead_letter(proj, dead_letter_id)
        if row is None or row.get("stream") != name:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"dead letter {dead_letter_id} not found"
            )
        show_payload = include_payload and caller is not None and "write" in caller.scopes
        if show_payload:
            write_audit_event(
                "dataplane",
                _actor(caller),
                "dataplane_stream_dlq_payload_read",
                f"{proj or '_global'}/{name}",
                {"id": row["id"], "project": proj, "stream": name},
            )
            return row
        return {k: v for k, v in row.items() if k != "payload"}

    @router.post("/streams/{name}/dead-letters/{dead_letter_id}/replay")
    def replay_dead_letter_route(
        name: str,
        dead_letter_id: int,
        request: Request,
        project: str = "",
        force: bool = False,
        caller: Caller = Depends(authenticate_write),
    ) -> Response:
        """Re-offer a stored dead letter to this stream through the running ingress
        (:func:`examlops.dataplane.streams.dlq.replay`); the outcome's own status, a lost claim
        answering 409."""
        from examlops.dataplane.streams import dlq as dlq_mod
        from examlops.dataplane.streams.bindings import get_binding

        proj = _stream_project(project)
        authorize_stream(caller, request, "write", proj, name)
        binding = get_binding(name, proj)
        if binding is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"stream {name!r} not found")
        row = dlq_mod.get_dead_letter(proj, dead_letter_id)
        if row is None or row.get("stream") != name:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"dead letter {dead_letter_id} not found"
            )
        try:
            stack = rt.stack()
        except Exception as exc:  # noqa: BLE001 - no stack: this replica cannot replay
            logger.warning(
                "dataplane: stream ingress unavailable for replay (%s)", type(exc).__name__
            )
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "the stream ingress is unavailable"
            ) from None
        from examlops.dataplane.streams.kafka_stream import EnvelopeRejected

        try:
            result = dlq_mod.replay(
                dead_letter_id,
                actor=caller.actor,
                ingress=stack.ingress,
                binding=binding,
                force=force,
            )
        except EnvelopeRejected as reject:
            # The commonest dead letter there is: a payload the envelope parser refuses (invalid
            # message, not JSON) — and one the binding's `max_bytes` no longer admits. `replay`
            # re-raises it after writing its audit row, so this is the route's to answer; without
            # this it reached FastAPI as a 500 with a traceback in the service log (review I1).
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"this dead letter cannot be replayed: {reject.reason}",
            ) from None
        except SpecError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, redact(str(exc))) from None
        return _replay_response(result, binding)

    @router.delete("/streams/{name}/dead-letters")
    def purge_dead_letters_route(
        name: str,
        request: Request,
        project: str = "",
        older_than: str = "",
        caller: Caller = Depends(authenticate_write),
    ) -> dict[str, Any]:
        """Purge this stream's dead letters older than ``older_than`` — an ISO-8601 duration
        (``P7D``, ``PT1H``) or a plain number of days."""
        from examlops.data.dataplane import get_stream
        from examlops.dataplane.streams.dlq import purge

        proj = _stream_project(project)
        authorize_stream(caller, request, "write", proj, name)
        if get_stream(name, proj) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"stream {name!r} not found")
        try:
            delta = _parse_older_than(older_than)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
        count = purge(proj, name, older_than=delta, actor=caller.actor)
        return {"project": proj, "name": name, "purged": count}


def _drain_step(what: str, fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - the drain always runs to its end
        logger.warning("dataplane: drain step %s failed (%s)", what, type(exc).__name__)


def _drain(rt: _StreamRuntime, scheduler: Any) -> None:
    """E10, in order, against ONE deadline (``rt.begin_drain()``: set at SIGTERM or here,
    whichever came first). Every wait uses only the time left; past the deadline only the two
    flushes still get :data:`_FLUSH_FLOOR_S` each, so the drain overruns its budget by at most
    about a second. Each step is guarded: one failing step never skips the next."""
    rt.begin_drain()  # 1. /ready and push answer 503 from here on (already, after SIGTERM)
    _drain_step("supervisor.stop", rt.stop_intake)  # 2. connectors stop and commit (once)
    if not rt.inflight.wait_idle(rt.remaining()):  # 3. in-flight pushes finish
        logger.warning(
            "dataplane: %d push request(s) still in flight at the drain deadline",
            rt.inflight.count,
        )
    stack = rt.built_stack()
    if stack is not None:  # 4. drift, then spool (flush), then client
        _drain_step("stack.close", lambda: stack.close(max(_FLUSH_FLOOR_S, rt.remaining() / 2)))
    supervisor = rt.supervisor
    if supervisor is not None:  # 5. the leader leases
        # signal-and-wait: each run gives its own lease back as it exits (never on its behalf)
        _drain_step("supervisor.release_leases", lambda: supervisor.release_leases(rt.remaining()))
    _drain_step("scheduler.stop", lambda: scheduler.stop(rt.remaining()))  # 6. the scheduler


def _stop_intake_quietly(rt: _StreamRuntime) -> None:
    _drain_step("supervisor.stop", rt.stop_intake)


def _install_sigterm(rt: _StreamRuntime) -> Callable[[], None]:
    """Chain a SIGTERM handler that starts the drain at once: it fixes the drain deadline, turns
    draining on (``/ready`` and push answer 503 while the server still finishes its connections)
    and stops connector intake on a background thread — then hands the signal to the handler that
    was there (the server's own, which then waits for in-flight HTTP up to its
    ``timeout_graceful_shutdown``, the same drain seconds). Main thread only; returns the undo."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    try:
        previous = signal.getsignal(signal.SIGTERM)
    except (ValueError, OSError):
        return lambda: None

    def _on_term(signum: int, frame: Any) -> None:
        rt.begin_drain()
        threading.Thread(
            target=_stop_intake_quietly, args=(rt,), name="dataplane-drain-intake", daemon=True
        ).start()
        if callable(previous):
            previous(signum, frame)
        elif previous == signal.SIG_DFL:  # nobody else handles it: keep the default (terminate)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.raise_signal(signum)

    try:
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError):
        return lambda: None

    def _undo() -> None:
        try:
            if signal.getsignal(signal.SIGTERM) is _on_term:
                signal.signal(signal.SIGTERM, previous)
        except (ValueError, OSError, TypeError):
            pass

    return _undo


def create_app(
    *,
    start_scheduler: bool = True,
    streams: bool | None = None,
    stack_factory: Callable[[], Any] | None = None,
    supervisor_factory: Callable[[Any, Any], Any] | None = None,
) -> FastAPI:
    """The dataplane service.

    ``streams`` switches the streaming surface — the push and stream routes (roles ``all``/
    ``api``) and the stream supervisor (roles ``all``/``streams``). ``None`` (the default) turns it
    on; the role alone then decides which parts this process runs. ``start_scheduler`` governs
    only the pull scheduler.
    ``stack_factory()`` builds the shared ingress stack (default ``IngressStack.build``);
    ``supervisor_factory(ingress, catalog_view)`` the supervisor (default a
    ``StreamSupervisor`` reading the catalog through the view). Both are for tests.
    """
    from prometheus_client import CollectorRegistry

    from examlops.dataplane.metrics import _metrics
    from examlops.dataplane.streams.supervisor import CatalogView, IngressStack, StreamSupervisor

    role = dataplane_role()  # an invalid role fails here, at startup
    streams_on = True if streams is None else streams
    mount_api = role in ("all", "api")
    mount_streams = streams_on and mount_api
    run_supervisor = streams_on and role in ("all", "streams")
    push_max_bytes = int(_env_number(PUSH_MAX_BYTES_ENV, DEFAULT_PUSH_MAX_BYTES))
    if push_max_bytes < 1:
        raise ValueError(f"{PUSH_MAX_BYTES_ENV} must be at least 1 byte, not {push_max_bytes}")
    drain_s = drain_seconds()
    push_read_timeout_s = _env_number(PUSH_READ_TIMEOUT_ENV, DEFAULT_PUSH_READ_TIMEOUT_S)
    if push_read_timeout_s <= 0:
        raise ValueError(f"{PUSH_READ_TIMEOUT_ENV} must be > 0 seconds, not {push_read_timeout_s}")

    scheduler = Scheduler(
        interval_s=_env_number("EXAMLOPS_DATAPLANE_SCHEDULER_INTERVAL", 30.0),
        workers=int(_env_number("EXAMLOPS_DATAPLANE_WORKERS", 2)),
    )
    # The per-source gauges live in this app's own registry (so creating several apps — tests —
    # never collides); the pull counters live in the default one. /metrics serves both.
    gauges = CollectorRegistry(auto_describe=False)
    gauges.register(_freshness_collector())
    _metrics()  # declare dataplane_pull_total & co. up front, so they exist before the first pull

    store_probe = _StoreProbe()
    catalog_view = CatalogView()
    rt = _StreamRuntime(
        stack_factory=stack_factory or IngressStack.build,
        catalog_view=catalog_view,
        push_max_bytes=push_max_bytes,
        drain_s=drain_s,
        push_read_timeout_s=push_read_timeout_s,
    )

    def _default_supervisor(ingress: Any, view: Any) -> Any:
        return StreamSupervisor(ingress, list_bindings=view.refresh)

    make_supervisor = supervisor_factory or _default_supervisor

    def _start_supervisor() -> None:
        stack = rt.stack()
        supervisor = make_supervisor(stack.ingress, catalog_view)
        supervisor.start()
        rt.supervisor = supervisor

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mode = auth_mode()  # never carries the token or the trust file's parse error
        if mode == "open" or "invalid" in mode:
            logger.warning("dataplane: auth mode %s", mode)
        else:
            logger.info("dataplane: auth mode %s", mode)
        # Task 22a: a process that was OOM-killed or crashed mid-pull leaves its catalog row
        # `running` forever and its stage dir behind — clean both up before serving traffic.
        # A `streams` process runs no pulls, so it leaves pull bookkeeping to the pull roles.
        from examlops.dataplane.pull import cleanup_stale_stage_dirs, reap_interrupted_pulls

        if mount_api:
            try:
                await asyncio.to_thread(reap_interrupted_pulls)
            except Exception:  # noqa: BLE001 — a datastore hiccup at startup must not crash
                logger.warning(
                    "dataplane: could not reap interrupted pulls at startup", exc_info=True
                )
            try:
                await asyncio.to_thread(cleanup_stale_stage_dirs)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "dataplane: could not clean stale stage dirs at startup", exc_info=True
                )
        if start_scheduler and mount_api:
            scheduler.start()
        if run_supervisor:
            try:
                await asyncio.to_thread(_start_supervisor)
            except Exception as exc:
                logger.error(
                    "dataplane: the stream supervisor could not start (%s)", type(exc).__name__
                )
                if role == "streams":
                    raise  # a streams-only process with no supervisor does nothing: fail loudly
        logger.info("dataplane: role %s", role)
        undo_sigterm = _install_sigterm(rt)
        try:
            yield
        finally:
            # E10: every step waits (bounded) — keep the whole drain off the event loop.
            try:
                await asyncio.to_thread(_drain, rt, scheduler)
            finally:
                undo_sigterm()

    app = FastAPI(title="ExaMLOps dataplane", version="1", lifespan=lifespan)
    app.state.scheduler = scheduler
    app.state.streams = rt
    app.state.role = role
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)

    @app.get("/health")
    def health() -> dict[str, Any]:
        from examlops.dataplane.connectors import registry

        db_ok = True
        try:
            catalog.list_sources()
        except Exception:  # noqa: BLE001 — reported as a component state, never raised
            db_ok = False
        store_ok = store_probe.check(_STORE_PROBE_TIMEOUT_S)
        connectors: dict[str, bool] = {}
        try:
            for c in registry.all_connectors():
                try:
                    connectors[c.kind] = bool(c.available()[0])
                except Exception:  # noqa: BLE001
                    connectors[c.kind] = False
        except Exception:  # noqa: BLE001
            pass
        return {
            "status": "ok" if db_ok and store_ok else "degraded",
            "db": db_ok,
            "store": store_ok,
            "auth": auth_mode(),
            "connectors": connectors,
            "role": role,
        }

    @app.get("/ready")
    def ready() -> Response:
        if rt.draining.is_set():  # E10: out of the load balancer before anything stops
            return JSONResponse({"status": "draining"}, status_code=503)
        return JSONResponse({"status": "alive"})

    @app.get("/metrics")
    def metrics() -> Response:
        from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

        return Response(
            generate_latest(REGISTRY) + generate_latest(gauges), media_type=CONTENT_TYPE_LATEST
        )

    # The source, pull and stream routes: roles `all` and `api` only (E11).
    api = APIRouter()

    @api.get("/connectors", dependencies=[Depends(require_read)])
    def connectors() -> list[dict[str, Any]]:
        from examlops.dataplane.connectors import registry

        rows = []
        for c in registry.all_connectors():
            ok, why = c.available()
            rows.append(
                {
                    "kind": c.kind,
                    "available": ok,
                    "detail": why,
                    "connection_kinds": list(c.connection_kinds),
                    "incremental": c.supports_incremental,
                    "extra": c.extra,
                }
            )
        return rows

    # Source-scoped routes authenticate in the dependency and authorise in the body, once the
    # source's project is known (for PUT and pull it sits in the request body): the project check
    # (spec §10) and the center's PDP with the real project run BEFORE the source is looked up,
    # so a caller without access gets one 403 whether or not the source exists (final review I12).

    @api.get("/sources")
    def sources(
        project: str | None = None, caller: Caller | None = Depends(require_read)
    ) -> list[dict[str, Any]]:
        if project is not None and not may_access_project(caller, "read", project):
            raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN_DETAIL)
        # One non-auditing check per distinct project, not one audited check per source.
        visible: dict[str, bool] = {}

        def _can_see(p: str) -> bool:
            if p not in visible:
                visible[p] = may_access_project(caller, "read", p, audit=False)
            return visible[p]

        return [_source(s) for s in dataplane.list_source_defs(project) if _can_see(s.project)]

    @api.get("/sources/{name}")
    def source(
        name: str,
        request: Request,
        project: str = "",
        caller: Caller | None = Depends(authenticate_read),
    ) -> dict[str, Any]:
        authorize_source(caller, request, "read", project, name)
        return _source(_existing(name, project))

    @api.put("/sources/{name}")
    def put_source(
        name: str, body: SourceBody, request: Request, caller: Caller = Depends(authenticate_write)
    ) -> dict[str, Any]:
        authorize_source(caller, request, "write", body.project, name)
        try:
            s = dataplane.define_source(
                name,
                body.connector,
                project=body.project,
                connection=body.connection,
                spec=body.spec,
                schedule=body.schedule,
                limits=Limits.from_dict(body.limits),
                contract=body.contract,
                enabled=body.enabled,
                actor=caller.actor,
            )
        except (DataplaneError, ValueError, TypeError) as exc:
            raise _bad_request(exc) from None
        return _source(s)

    @api.delete("/sources/{name}")
    def delete_source(
        name: str,
        request: Request,
        project: str = "",
        caller: Caller = Depends(authenticate_write),
    ) -> dict[str, Any]:
        authorize_source(caller, request, "write", project, name)
        try:
            removed = dataplane.remove_source(name, project, actor=caller.actor)
        except DataplaneError as exc:
            raise _bad_request(exc) from None
        if not removed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"source {name!r} not found")
        return {"deleted": name}

    # `test` and `preview` open outbound connections with the source's stored credentials (and
    # preview returns the rows), so they need the write scope — the same tier the CLI Console
    # gives `exa dataplane test/preview`.
    @api.post("/sources/{name}/test")
    def test_source(
        name: str,
        request: Request,
        project: str = "",
        caller: Caller = Depends(authenticate_write),
    ) -> dict[str, Any]:
        from examlops.connections import ConnectionError as NamedConnectionError

        authorize_source(caller, request, "write", project, name)
        _existing(name, project)
        try:
            probe = dataplane.probe_source(name, project=project)
        except (DataplaneError, NamedConnectionError) as exc:  # e.g. the connection is gone
            raise _bad_request(exc) from None
        return {"ok": probe.ok, "detail": redact(probe.detail)}

    @api.post("/sources/{name}/preview")
    def preview(
        name: str,
        request: Request,
        project: str = "",
        limit: int = Query(20, ge=1, le=1000),
        caller: Caller = Depends(authenticate_write),
    ) -> list[dict[str, Any]]:
        from examlops.connections import ConnectionError as NamedConnectionError

        authorize_source(caller, request, "write", project, name)
        _existing(name, project)
        try:
            rows = dataplane.preview(name, project=project, limit=limit)
        except (DataplaneError, NamedConnectionError) as exc:
            raise _bad_request(exc) from None
        # Rows carry whatever the source holds (timestamps, decimals, bytes): make them JSON.
        result: list[dict[str, Any]] = json.loads(json.dumps(rows, default=str))
        return result

    @api.post("/sources/{name}/pull", status_code=status.HTTP_202_ACCEPTED)
    def pull(
        name: str,
        request: Request,
        body: PullBody | None = None,
        caller: Caller = Depends(authenticate_write),
    ) -> dict[str, str]:
        body = body or PullBody()
        authorize_source(caller, request, "write", body.project, name)
        src = _existing(name, body.project)
        if not src.enabled:
            raise HTTPException(status.HTTP_409_CONFLICT, f"source {src.key} is disabled")
        try:
            pull_id = scheduler.submit(
                name, body.project, trigger_kind="api", full=body.full, actor=_actor(caller)
            )
        except SpecError as exc:  # removed between the check and the reservation
            raise HTTPException(status.HTTP_404_NOT_FOUND, redact(str(exc))) from None
        if pull_id is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"a pull of {src.key} is already queued or running"
            )
        return {"pull_id": pull_id}

    @api.get("/pulls/{pull_id}")
    def get_pull(
        pull_id: str, request: Request, caller: Caller | None = Depends(authenticate_read)
    ) -> dict[str, Any]:
        # accepted but not started, or failed before it; else it may have started between the two
        found = (
            catalog.get_pull(pull_id) or scheduler.status_of(pull_id) or catalog.get_pull(pull_id)
        )
        # Another project's pull is reported exactly like an unknown one (404), so the route does
        # not reveal that it exists.
        if found is None or not may_access_project(
            caller, "read", found.get("project") or "", audit=False
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"pull {pull_id!r} not found")
        authorize_source(caller, request, "read", found.get("project") or "", found.get("source"))
        return dict(found)

    @api.get("/sources/{name}/snapshots")
    def snapshots(
        name: str,
        request: Request,
        project: str = "",
        limit: int = Query(200, ge=1, le=1000),
        caller: Caller | None = Depends(authenticate_read),
    ) -> list[dict[str, Any]]:
        authorize_source(caller, request, "read", project, name)
        _existing(name, project)
        return [
            r
            for r in catalog.list_pulls(project=project, source=name, limit=limit)
            if r["status"] in _COMMITTED and r.get("revision")
        ]

    if mount_streams:
        _mount_stream_routes(api, rt)
    if mount_api:
        app.include_router(api)
    return app


__all__ = ["ROLES", "configure_logging", "create_app", "dataplane_role", "drain_seconds"]
