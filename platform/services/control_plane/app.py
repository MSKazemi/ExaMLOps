"""
ExaMLOps Control Plane — retraining trigger API.

Reliability improvements batch 1 (2026-06-12 round 1):
  1.  Registry TTL cache (60 s) — eliminates per-request YAML disk reads
  2.  SQLite WAL mode + indices — better concurrency and faster hot queries
  3.  Prefect gateway retry (3 attempts, exponential backoff) — tolerates blips
  4.  Concurrent /status pings — worst-case 5 s instead of 25 s
  5.  Retrain deduplication — shared lease plus durable command idempotency
  6.  Write-endpoint rate limiting — shared fixed window, 429 with Retry-After
  7.  Poller health tracking — last-ok timestamp in /health
  8.  lifespan context manager + graceful shutdown — replaces deprecated on_event
  9.  Startup validation — db/registry/token checks exposed in /health
  10. Poller uses threading.Event.wait() for clean stop on shutdown

Production-grade improvements batch 2 (2026-06-12 round 2):
  11. Structured JSON logging — LOG_FORMAT=json for Loki ingestion
  12. Prefect circuit breaker — fast-fails after N consecutive errors, half-open recovery
  13. Retrain Prometheus metrics — counter + histogram for retrain requests
  14. Request-ID middleware — X-Request-ID propagated through all logs and responses
  15. Liveness / readiness split — GET /ready (instant) vs GET /health (detailed)
  16. Approval expiry — APPROVAL_EXPIRY_HOURS background cleanup, rejects stale entries
  17. Idempotency keys — X-Idempotency-Key deduplicates client retries on /retrain
  18. Security-headers middleware — X-Content-Type-Options, X-Frame-Options, Cache-Control
  19. Config hot-reload — POST /admin/reload invalidates cache + re-runs startup checks
  20. DB NFS retry — sqlite3.OperationalError retried 3× for NFS-hosted DB resilience

Env vars (all existing + new):
    CONTROL_PLANE_PORT                default: 8002
    CONTROL_PLANE_TOKEN               legacy operator bearer token (read + write, default tenant)
    CONTROL_PLANE_CREDENTIALS_JSON    optional token-keyed principal/tenant/scopes credential map
    PREFECT_API_URL                   default: http://localhost:14200/api  (the host port;
                                      compose sets http://orchestrator:4200/api itself)
    PREFECT_DEPLOYMENT_NAME           default: training_flow/examlops-dispatch
    CONTROL_PLANE_DB                  default: /data/approvals.db
    EXAMLOPS_COORDINATOR              default: db  (redis for cross-host coordination)
    EXAMLOPS_EVENT_PUBLISHER          default: log
    CONTROL_PLANE_EVENT_RELAY_SECONDS default: 1  (0 disables the relay)
    RETRAIN_RATE_LIMIT_PER_MIN        default: 20
    LOG_FORMAT                        default: text  (set to "json" in production)
    APPROVAL_EXPIRY_HOURS             default: 72  (0 = disabled)
    PREFECT_CB_FAIL_MAX               default: 5   circuit breaker open threshold
    PREFECT_CB_RESET_TIMEOUT          default: 30  seconds before half-open retry
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac as _hmac
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, TypeVar

import metrics as _metrics
import uvicorn

# The control plane's modules (plan P1.1). Re-exported here so `app.<name>` — which callers and
# tests use — keeps resolving to the same objects.
from cplane import policy_gate as _policy_gate
from cplane import problems as _problems
from cplane import project_gate as _project_gate
from cplane.gateway import (  # noqa: F401
    PREFECT_BACKOFF_BASE,
    PREFECT_CALL_BUDGET,
    PREFECT_CONNECT_TIMEOUT,
    PREFECT_MAX_ATTEMPTS,
    PREFECT_READ_TIMEOUT,
    PrefectGateway,
    _CircuitBreaker,
    _dispatch_budget,
    _dispatch_deadline,
    _prefect_breaker,
)
from cplane.models import (  # noqa: F401
    ApprovalEntry,
    ChangeNotification,
    CommandPage,
    CommandView,
    FlowRunStatus,
    ModelEntry,
    RejectRequest,
    RetrainRequest,
    RetrainResponse,
)
from cplane.schema import (  # noqa: F401
    _CREATE_ADMISSION_SQL,
    _CREATE_COMMANDS_SQL,
    _CREATE_INDICES_SQL,
    _CREATE_MODEL_FRESHNESS_SQL,
    _CREATE_MODELZOO_EVENTS_SQL,
    _CREATE_OUTBOX_SQL,
    _CREATE_SETTINGS_SQL,
    _CREATE_TABLE_SQL,
    _SCHEMA_COLUMNS,
    _apply_schema_migrations,
)
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from examlops.admission import max_running as admission_max_running
from examlops.admission import per_tenant_cap as admission_per_tenant_cap
from examlops.coordination import get_coordinator as _selected_coordinator
from examlops.data.audit import append_audit_event
from examlops.data.events import enqueue_event
from examlops.data.events import outbox_oldest_pending_age as _shared_outbox_oldest_age
from examlops.data.events import outbox_stats as _shared_outbox_stats
from examlops.events import get_publisher as _selected_publisher
from examlops.events import relay_once as _shared_relay_once
from examlops.platform_db import begin_immediate
from examlops.secrets.inject import inject_env as _inject_secret_refs
from examlops.storage import PostgresBackend, SqliteBackend

CONTROL_PLANE_STATE_BACKEND = os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower()
CONTROL_PLANE_DB = os.getenv("CONTROL_PLANE_DB") or os.getenv("PLATFORM_DB") or "/data/approvals.db"
# Shared DB-backed coordination and the stock outbox relay resolve SQLite through PLATFORM_DB.
# This service owns one transactional state boundary, so make its explicit database authoritative
# inside this process. Postgres ignores file paths and already converges on DSN + schema.
if CONTROL_PLANE_STATE_BACKEND == "sqlite":
    os.environ["PLATFORM_DB"] = CONTROL_PLANE_DB
# Pinned BEFORE secret injection below: the local secrets store and the injection audit both
# live in PLATFORM_DB, so resolving first would read (and audit into) a different database.

# ADR 0011 clause 2 — resolve `secret://` / `secret+file://` references in the environment before
# any credential below is read (CONTROL_PLANE_TOKEN, CONTROL_PLANE_CREDENTIALS_JSON, …). A no-op
# when the environment holds no reference; an unresolvable one refuses to start (fail closed).
_inject_secret_refs("control-plane")

T = TypeVar("T")

# ─── Improvement 11: Structured JSON logging ─────────────────────────────────

_LOG_FORMAT = os.getenv("LOG_FORMAT", "text").lower()


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object for Loki ingestion."""

    _SKIP = frozenset(
        (
            "args",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelno",
            "lineno",
            "message",
            "module",
            "msecs",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "thread",
            "threadName",
        )
    )

    def format(self, record: logging.LogRecord) -> str:
        d: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        req_id = _request_id_var.get("")
        if req_id:
            d["request_id"] = req_id
        for k, v in record.__dict__.items():
            if k not in self._SKIP and not k.startswith("_"):
                try:
                    json.dumps(v)
                    d[k] = v
                except (TypeError, ValueError):
                    d[k] = str(v)
        return json.dumps(d, separators=(",", ":"))


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    if _LOG_FORMAT == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [control-plane] %(levelname)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


_configure_logging()
logger = logging.getLogger("control_plane")

try:
    import model_meta as _model_meta_mod
except ImportError:
    _model_meta_mod = None  # type: ignore


# ─── Config ──────────────────────────────────────────────────────────────────

CONTROL_PLANE_PORT = int(os.getenv("CONTROL_PLANE_PORT", "8002"))
CONTROL_PLANE_TOKEN = os.getenv("CONTROL_PLANE_TOKEN", "")

# Well-known placeholder tokens are rejected outright (fail closed): a deploy that ships with an
# example/default token is effectively unauthenticated (Phase 0 item 0.8 / QW6).
_WEAK_TOKENS = frozenset(
    {
        "changeme",
        "change-me",
        "change_me",
        "changethis",
        "changeme123",
        "placeholder",
        "example",
        "your-token-here",
        "yourtoken",
        "todo",
        "none",
        "null",
    }
)


# The same placeholders as they appear *decorated* in real example files. `.env.example` ships
# `change-me-control-plane-token`, which an exact-match list never catches — and a shipped default
# that passes the guard is worse than no guard, because /health then reports auth_configured: true.
# Every marker here is long enough not to occur by accident inside a random secret.
_WEAK_MARKERS = (
    "changeme",
    "change-me",
    "change_me",
    "changethis",
    "placeholder",
    "your-token",
    "yourtoken",
    "replace-me",
    "replaceme",
)


# The shared legacy token grants read and write — every action — to one principal, however
# narrowly the per-service credentials are scoped (plan P3.2). `on` keeps it; `warn` keeps it but
# counts and logs each use, to find who still depends on it; `off` refuses it. Anything else is
# treated as `off`, and the startup check says so: a typo must not leave the master key working.
_LEGACY_MODES = frozenset({"on", "warn", "off"})
_LEGACY_MODE_RAW = os.getenv("CONTROL_PLANE_LEGACY_TOKEN", "on").strip().lower()
LEGACY_TOKEN_MODE = _LEGACY_MODE_RAW if _LEGACY_MODE_RAW in _LEGACY_MODES else "off"
_legacy_warned_at = 0.0


def _token_is_usable() -> bool:
    """True only when a real token is configured — not unset, not a placeholder, not retired."""
    return LEGACY_TOKEN_MODE != "off" and _secret_is_usable(CONTROL_PLANE_TOKEN)


def _note_legacy_use(request: Any) -> None:
    """Count every use of the legacy token; in `warn` mode, also say who (at most once a minute)."""
    global _legacy_warned_at
    _metrics.record_legacy_token_use()
    if LEGACY_TOKEN_MODE != "warn":
        return
    now = time.monotonic()
    if now - _legacy_warned_at < 60:
        return
    _legacy_warned_at = now
    client = getattr(getattr(request, "client", None), "host", None) or "unknown"
    agent = request.headers.get("user-agent", "unknown") if isinstance(request, Request) else "?"
    path = request.url.path if isinstance(request, Request) else "?"
    logger.warning(
        "The legacy CONTROL_PLANE_TOKEN was used (client %s, agent %s, %s). Give that caller its "
        "own credential in CONTROL_PLANE_CREDENTIALS_JSON; CONTROL_PLANE_LEGACY_TOKEN=off will "
        "refuse it.",
        client,
        agent,
        path,
    )


def _secret_is_usable(value: str) -> bool:
    t = value.strip().lower()
    if not t or t in _WEAK_TOKENS:
        return False
    return not any(m in t for m in _WEAK_MARKERS)


@dataclass(frozen=True)
class RequestContext:
    principal: str
    tenant: str
    scopes: frozenset[str]
    is_legacy: bool = False
    # The verified `examlops.iam.Principal` when the caller authenticated with a token from a
    # trusted data-center IdP (ADR 0120); None for static credentials.
    identity: Any = field(default=None, compare=False, repr=False)
    # How the caller proved itself: static | legacy | workload | federated. Audited and counted,
    # so a service's static secret can be retired once it has moved to its workload identity.
    credential: str = field(default="static", compare=False)
    spiffe_id: str | None = field(default=None, compare=False)


def _credential_details(context: RequestContext) -> dict[str, str]:
    """What the audit records about how the actor authenticated (ADR 0125)."""
    details = {"credential": context.credential}
    if context.spiffe_id:
        details["spiffe_id"] = context.spiffe_id
    return details


# Scopes a static credential may carry (plan P3.2). `write` is every mutation, as before; the
# narrower ones let a service hold exactly the action it performs — the Dataplane bus bridge and the
# autopilot need `retrain`, CI needs `changes`, and neither should be able to approve a model or
# reconfigure the platform.
ACTION_SCOPES = {
    "retrain": "request retrains (POST /retrain, /v1/retrain) and cancel queued commands",
    "approve": "approve, reject or retract pending approvals",
    "changes": "report CI model changes that open approvals (POST /api/changes)",
    "admin": "reload the registry and change or sync the ModelZoo integration",
}
SCOPES = frozenset({"read", "write", *ACTION_SCOPES})


def _parse_credentials(raw: str) -> tuple[dict[str, RequestContext], str | None]:
    if not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("must be a non-empty JSON object keyed by bearer token")
        credentials: dict[str, RequestContext] = {}
        for token, value in parsed.items():
            if not isinstance(token, str) or not _secret_is_usable(token):
                raise ValueError("each bearer token must be a non-placeholder string")
            if not isinstance(value, dict):
                raise ValueError("each credential value must be an object")
            principal = value.get("principal")
            tenant = value.get("tenant")
            scopes = value.get("scopes")
            if not isinstance(principal, str) or not principal.strip():
                raise ValueError("each credential requires a non-empty principal")
            if not isinstance(tenant, str) or not tenant.strip():
                raise ValueError("each credential requires a non-empty tenant")
            if not isinstance(scopes, list) or not scopes:
                raise ValueError("each credential requires a non-empty scopes list")
            scope_set = frozenset(scopes)
            if any(not isinstance(scope, str) for scope in scopes) or not scope_set <= SCOPES:
                raise ValueError(
                    "credential scopes must be drawn from " + ", ".join(sorted(SCOPES))
                )
            credentials[token] = RequestContext(
                principal=principal.strip(), tenant=tenant.strip(), scopes=scope_set
            )
        return credentials, None
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {}, str(exc)


_structured_credentials, _credential_config_error = _parse_credentials(
    os.getenv("CONTROL_PLANE_CREDENTIALS_JSON", "")
)
if (
    _credential_config_error is None
    and _token_is_usable()
    and any(_hmac.compare_digest(token, CONTROL_PLANE_TOKEN) for token in _structured_credentials)
):
    _structured_credentials = {}
    _credential_config_error = "legacy and structured bearer credentials must be distinct"


def _parse_workload_identities(raw: str) -> tuple[dict[str, RequestContext], str | None]:
    """SPIFFE ID → the principal, tenant and scopes that workload acts with (ADR 0125)."""
    if not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("must be a non-empty JSON object keyed by SPIFFE ID")
        workloads: dict[str, RequestContext] = {}
        for spiffe_id, value in parsed.items():
            if not isinstance(spiffe_id, str) or not spiffe_id.startswith("spiffe://"):
                raise ValueError(f"{spiffe_id!r} is not a SPIFFE ID (spiffe://<domain>/<path>)")
            if not isinstance(value, dict):
                raise ValueError("each workload value must be an object")
            principal, tenant, scopes = (
                value.get("principal"),
                value.get("tenant"),
                value.get("scopes"),
            )
            if not isinstance(principal, str) or not principal.strip():
                raise ValueError("each workload requires a non-empty principal")
            if not isinstance(tenant, str) or not tenant.strip():
                raise ValueError("each workload requires a non-empty tenant")
            if not isinstance(scopes, list) or not scopes or not set(scopes) <= SCOPES:
                raise ValueError("workload scopes must be drawn from " + ", ".join(sorted(SCOPES)))
            workloads[spiffe_id] = RequestContext(
                principal=principal.strip(), tenant=tenant.strip(), scopes=frozenset(scopes)
            )
        return workloads, None
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {}, str(exc)


_workload_identities, _workload_config_error = _parse_workload_identities(
    os.getenv("CONTROL_PLANE_WORKLOAD_IDENTITIES_JSON", "")
)
# The audience a JWT-SVID must name to be accepted here; one per receiving service.
SPIFFE_AUDIENCE = os.getenv("CONTROL_PLANE_SPIFFE_AUDIENCE", "control-plane").strip()
_metrics.initialize_authentications(
    [(c.principal, "static") for c in _structured_credentials.values()]
    + [(c.principal, "workload") for c in _workload_identities.values()]
    + ([("legacy", "legacy")] if _token_is_usable() else [])
)


def _workload_context(token: str) -> RequestContext:
    """The context of a verified, mapped JWT-SVID; 403 otherwise (never the IdP path)."""
    from examlops import workload_identity  # noqa: PLC0415

    try:
        workload = workload_identity.verify(token, SPIFFE_AUDIENCE)
    except workload_identity.WorkloadIdentityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Invalid workload identity: {exc}") from exc
    context = _workload_identities.get(workload.spiffe_id)
    if context is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"{workload.spiffe_id} is not a known workload"
        )
    return replace(context, credential="workload", spiffe_id=workload.spiffe_id)


def _iam_status() -> str:
    """Identity federation (ADR 0120): ``off`` | ``ok`` | ``fail: <why>`` — never raises.

    The trust file is re-read when it changes on disk, so onboarding a data center's IdP does not
    need a restart. An invalid trust file is ``fail`` and federated tokens are refused (fail
    closed); static credentials keep working.
    """
    try:
        from examlops.iam import IamConfigError, load_config
    except ImportError:
        return "off"
    try:
        return "ok" if load_config().enabled else "off"
    except IamConfigError as exc:
        return f"fail: {exc}"


def _auth_is_usable() -> bool:
    return _credential_config_error is None and (
        bool(_structured_credentials)
        or _token_is_usable()
        or _iam_status() == "ok"
        or bool(_workload_identities)
    )


# 4200 is Prefect's *container* port. On this host the stack publishes it as 14200
# (`14200:4200` in docker-compose.yml), and inside the compose network the address is
# `http://orchestrator:4200/api`, which compose sets explicitly. So the old default —
# loopback plus the container port — was right in neither place: a control plane started
# outside compose reported a healthy Prefect as down on `/status`, and `PrefectGateway`
# posted its retrain flow runs into nothing.
PREFECT_API_URL = os.getenv("PREFECT_API_URL", "http://localhost:14200/api").rstrip("/")
# `training_flow/examlops-dispatch` is registered and served by `exa pipeline deploy`
# (pipelines/deploy.py DISPATCH_DEPLOYMENT_NAME); tests/unit/test_dispatch_contract.py keeps this
# default, the compose default and that constant identical.
PREFECT_DEPLOYMENT_NAME = os.getenv("PREFECT_DEPLOYMENT_NAME", "training_flow/examlops-dispatch")
MODELZOO_WEBHOOK_SECRET = os.getenv("MODELZOO_WEBHOOK_SECRET", "")
MODELZOO_AUTO_RETRAIN = os.getenv("MODELZOO_AUTO_RETRAIN", "false").lower() == "true"
MODELZOO_POLL_SECONDS = int(os.getenv("MODELZOO_POLL_SECONDS", "300"))
MODELZOO_WATCH_BRANCH = os.getenv("MODELZOO_WATCH_BRANCH", "main")
GITLAB_URL = os.getenv("GITLAB_URL", "https://gitlab.com")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN", "")
GITLAB_PROJECT_ID = os.getenv("GITLAB_PROJECT_ID", "")
MLFLOW_URL = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:15000")
RAY_SERVE_URL = os.getenv("RAY_SERVE_URL", "http://localhost:18001")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:18099")
RETRAIN_RATE_LIMIT_PER_MIN = int(os.getenv("RETRAIN_RATE_LIMIT_PER_MIN", "20"))
# Improvement 16
APPROVAL_EXPIRY_HOURS = int(os.getenv("APPROVAL_EXPIRY_HOURS", "72"))
# Improvement 12
PREFECT_CB_FAIL_MAX = int(os.getenv("PREFECT_CB_FAIL_MAX", "5"))
PREFECT_CB_RESET_TIMEOUT = float(os.getenv("PREFECT_CB_RESET_TIMEOUT", "30.0"))
# A process may disappear after claiming a command but before recording Prefect's response. The
# replacement process may reclaim it after this lease and re-submit with the same Prefect
# idempotency key, which is safe even when the first POST reached Prefect before the crash. Every
# dispatch ends within CONTROL_PLANE_DISPATCH_BUDGET_SECONDS (8), so the lease only has to outlast
# that: 60 s. It was 300, and the failover drill showed what that costs: a crashed replica's
# queued retrains waited five minutes before another replica took them over.
COMMAND_LEASE_SECONDS = max(1, int(os.getenv("CONTROL_PLANE_COMMAND_LEASE_SECONDS", "60")))
RETRAIN_LOCK_SECONDS = max(
    60, int(os.getenv("CONTROL_PLANE_RETRAIN_LOCK_SECONDS", str(COMMAND_LEASE_SECONDS)))
)
POLLER_LEASE_SECONDS = max(3, int(os.getenv("CONTROL_PLANE_POLLER_LEASE_SECONDS", "30")))
EVENT_RELAY_SECONDS = max(0.0, float(os.getenv("CONTROL_PLANE_EVENT_RELAY_SECONDS", "1")))
EVENT_RELAY_BATCH_SIZE = max(1, int(os.getenv("CONTROL_PLANE_EVENT_RELAY_BATCH_SIZE", "100")))
# The serving-snapshot projector (plan P4.2): full recompile interval (0 disables it) and how
# often it checks whether a serving-relevant event arrived since the last compile.
SNAPSHOT_SECONDS = max(0.0, float(os.getenv("CONTROL_PLANE_SNAPSHOT_SECONDS", "60")))
SNAPSHOT_TICK_SECONDS = max(0.2, float(os.getenv("CONTROL_PLANE_SNAPSHOT_TICK_SECONDS", "1")))
# How often every model's prediction-drift status is scored and a change announced as
# drift.status_changed (plan P2.4b). 0 disables it.
DRIFT_EVAL_SECONDS = max(0.0, float(os.getenv("CONTROL_PLANE_DRIFT_EVAL_SECONDS", "60")))
# ADR 0017 clause 4: how often the control plane checks for feature views whose materialization
# interval has elapsed (0 disables). Only views declaring an interval are ever materialized.
FEATURE_MATERIALIZE_SECONDS = max(
    0.0, float(os.getenv("CONTROL_PLANE_FEATURE_MATERIALIZE_SECONDS", "300"))
)
# How often the relay loop reads consumer lag and dead-letter depth from JetStream (P2.7).
EVENT_BACKBONE_STATS_SECONDS = max(
    5.0, float(os.getenv("CONTROL_PLANE_EVENT_BACKBONE_STATS_SECONDS", "30"))
)
# When set, a modelzoo push event triggers the ai-production CI pipeline automatically.
AI_PROD_PROJECT_ID = os.getenv("AI_PROD_GITLAB_PROJECT_ID", "")
AI_PROD_PIPELINE_TOKEN = os.getenv("AI_PROD_PIPELINE_TRIGGER_TOKEN", "")

# The environment's defaults. What a replica acts on is ``_modelzoo_settings()``: these, overlaid by
# the values an operator set through PUT /v1/modelzoo/config, which live in the shared state store.
_modelzoo_config: dict[str, Any] = {
    "auto_retrain": MODELZOO_AUTO_RETRAIN,
    "poll_interval_seconds": MODELZOO_POLL_SECONDS,
    "watch_branch": MODELZOO_WATCH_BRANCH,
}
# How long a replica may act on its cached copy of the shared settings before re-reading them.
SETTINGS_TTL_SECONDS = max(0.0, float(os.getenv("CONTROL_PLANE_SETTINGS_TTL_SECONDS", "5")))
_settings_cache: dict[str, Any] = {"at": float("-inf"), "values": {}}
_instance_id = uuid.uuid4().hex


# ─── Improvement 14: Request-ID context var ──────────────────────────────────

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


# ─── Shared coordination (rate limits, retrain locks, poller lease) ──────────


def _get_coordinator() -> Any:
    """Return the configured shared coordinator (DB or Redis)."""
    return _selected_coordinator()


# ─── Improvement 1: Registry TTL cache ───────────────────────────────────────


@dataclass
class _RegistryCache:
    data: dict[str, list[str]]
    expires_at: float


_registry_cache: _RegistryCache | None = None
_REGISTRY_LOCK = threading.Lock()
_REGISTRY_TTL = 60.0


def _get_registry() -> dict[str, list[str]]:
    global _registry_cache
    with _REGISTRY_LOCK:
        now = time.monotonic()
        if _registry_cache is None or now >= _registry_cache.expires_at:
            _registry_cache = _RegistryCache(data=_load_registry(), expires_at=now + _REGISTRY_TTL)
        return _registry_cache.data


def _invalidate_registry_cache() -> None:
    global _registry_cache
    with _REGISTRY_LOCK:
        _registry_cache = None


# ─── SQLite approval store ────────────────────────────────────────────────────

_CONFIG_LOCK = threading.Lock()

_platform_schema_ready = False
_PLATFORM_SCHEMA_LOCK = threading.Lock()


def _ensure_platform_schema() -> None:
    """Create the shared platform tables this service writes into (``audit_events``) once.

    Decisions are audited on the command's own connection (plan P0.8), which cannot bootstrap a
    schema mid-transaction. Doing it on first use rather than in the lifespan hook keeps every
    entry point honest — tests and harnesses that mount the app without its lifespan included.
    """
    global _platform_schema_ready
    if _platform_schema_ready:
        return
    with _PLATFORM_SCHEMA_LOCK:
        if not _platform_schema_ready:
            from examlops.platform_db import init_db  # noqa: PLC0415

            init_db()
            _platform_schema_ready = True


_cp_schema_ready: set[str] = set()
_CP_SCHEMA_LOCK = threading.Lock()


def _schema_key() -> str:
    if CONTROL_PLANE_STATE_BACKEND == "postgres":
        return "pg:{}:{}".format(
            os.getenv("EXAMLOPS_POSTGRES_DSN", ""), os.getenv("EXAMLOPS_POSTGRES_SCHEMA", "")
        )
    return f"sqlite:{os.path.realpath(CONTROL_PLANE_DB)}"


def _apply_cp_schema(conn: Any) -> None:
    """Create/migrate the control plane's own tables and indices on ``conn``, then commit."""
    if CONTROL_PLANE_STATE_BACKEND == "postgres":
        # Replicas booting together on an empty Postgres race IF NOT EXISTS (duplicate key on
        # pg_type); the same advisory lock the platform schema bootstrap takes serialises them.
        conn.execute(begin_immediate("schema"))
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(_CREATE_MODELZOO_EVENTS_SQL)
    conn.execute(_CREATE_MODEL_FRESHNESS_SQL)
    conn.execute(_CREATE_COMMANDS_SQL)
    conn.execute(_CREATE_ADMISSION_SQL)
    conn.execute(_CREATE_OUTBOX_SQL)
    conn.execute(_CREATE_SETTINGS_SQL)
    _apply_schema_migrations(conn)
    for idx_sql in _CREATE_INDICES_SQL:
        conn.execute(idx_sql)
    conn.commit()


def _ensure_cp_schema(conn: Any) -> None:
    """Run the schema DDL once per database per process (plan P1.4 / finding P2).

    It used to run on **every** connection — six CREATE TABLEs, a PRAGMA-driven migration and six
    index statements on each request, health probe and poll, with a commit (and on SQLite a write
    lock) every time. Schema changes arrive with a new release, which is a new process.
    """
    key = _schema_key()
    if key in _cp_schema_ready:
        return
    with _CP_SCHEMA_LOCK:
        if key not in _cp_schema_ready:
            _apply_cp_schema(conn)
            _cp_schema_ready.add(key)


def _get_db() -> Any:
    """Open the configured control-plane state store.

    SQLite remains the local-development backend and retains the WAL/retry behaviour. Production
    may select the existing shared Postgres adapter with ``EXAMLOPS_DB_BACKEND=postgres`` and
    ``EXAMLOPS_POSTGRES_DSN``. Keeping this behind one connection function lets the endpoint
    contracts remain unchanged while removing the control plane's private persistence island.
    """
    if CONTROL_PLANE_STATE_BACKEND == "postgres":
        _ensure_platform_schema()
        conn = PostgresBackend().connect()
        _ensure_cp_schema(conn)
        return conn

    if CONTROL_PLANE_STATE_BACKEND != "sqlite":
        raise RuntimeError(
            "Unsupported EXAMLOPS_DB_BACKEND for the control plane: "
            f"{CONTROL_PLANE_STATE_BACKEND!r}; expected 'sqlite' or 'postgres'"
        )

    db_path = CONTROL_PLANE_DB
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    last_exc: Exception | None = None
    for attempt, delay in enumerate([0.0, 0.1, 0.3], start=1):
        if delay:
            time.sleep(delay)
        try:
            conn = SqliteBackend(db_path).connect()
            conn.execute("PRAGMA synchronous=NORMAL")  # per connection; WAL persists in the file
            if _schema_key() not in _cp_schema_ready:
                conn.execute("PRAGMA journal_mode=WAL")
            _ensure_cp_schema(conn)
            # Inside the retried block: a transient failure here is the same NFS/lock blip the
            # retry exists for.
            _ensure_platform_schema()
            return conn
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if attempt < 3:
                logger.warning("SQLite open failed (attempt %d/3): %s — retrying", attempt, exc)

    raise sqlite3.OperationalError(f"DB unavailable after 3 attempts: {last_exc}") from last_exc


# ─── Improvement 9 + 19: Startup validation ──────────────────────────────────

_startup_checks: dict[str, str] = {}
_startup_checked_at = 0.0
_STARTUP_RECHECK_LOCK = threading.Lock()
# How often /health and /readyz may re-run a failed startup check.
STARTUP_RECHECK_SECONDS = max(1.0, float(os.getenv("CONTROL_PLANE_STARTUP_RECHECK_SECONDS", "10")))


# The checks that decide readiness. Everything else is reported in /health and leaves the replica
# in rotation (see health()).
_READINESS_CHECKS = ("db", "token", "coordinator")


def _run_startup_checks(*, recheck: bool = False) -> None:
    """Evaluate the checks /health and /readyz report. ``recheck`` is a re-evaluation of a failed
    check (see `_recheck_failed_startup`): it logs only if the outcome changed, since a dependency
    that stays down would otherwise log the same error on every probe."""
    global _startup_checks, _startup_checked_at
    _err = logger.debug if recheck else logger.error
    _warn = logger.debug if recheck else logger.warning
    checks: dict[str, str] = {}

    try:
        conn = _get_db()
        conn.execute("SELECT COUNT(*) FROM pending_approvals")
        conn.close()
        checks["db"] = "ok"
    except Exception as exc:
        _err("Startup check FAILED — db: %s", exc)
        checks["db"] = f"fail: {exc}"

    try:
        reg = _load_registry()
        checks["registry"] = "ok" if reg else "warn: no enabled models"
        if not reg:
            _warn("Startup check WARN — registry: no enabled models found")
    except Exception as exc:
        _err("Startup check FAILED — registry: %s", exc)
        checks["registry"] = f"fail: {exc}"

    if _credential_config_error is not None:
        checks["token"] = (
            f"fail: malformed CONTROL_PLANE_CREDENTIALS_JSON: {_credential_config_error}"
        )
        _err("Startup check FAILED — credential map is malformed")
    elif _auth_is_usable():
        checks["token"] = "ok"
    elif CONTROL_PLANE_TOKEN.strip() and LEGACY_TOKEN_MODE == "off":
        checks["token"] = "missing"
        _err(
            "Startup check FAILED — the legacy token is disabled (CONTROL_PLANE_LEGACY_TOKEN=off) "
            "and no other credential is configured"
        )
    elif CONTROL_PLANE_TOKEN.strip():
        checks["token"] = "weak"
        _err(
            "Startup check FAILED — CONTROL_PLANE_TOKEN is a known placeholder "
            "(e.g. 'changeme'); protected endpoints return 503 until a real credential is set"
        )
    else:
        checks["token"] = "missing"
        _err("Startup check FAILED — no control-plane bearer credential is configured")

    if _workload_config_error is not None:
        checks["workload_identity"] = (
            f"fail: malformed CONTROL_PLANE_WORKLOAD_IDENTITIES_JSON: {_workload_config_error}"
        )
        _err("Startup check FAILED — the workload identity map is malformed")
    elif _workload_identities:
        from examlops import workload_identity  # noqa: PLC0415

        if not workload_identity.enabled():
            checks["workload_identity"] = (
                "fail: workloads are mapped but EXAMLOPS_SPIFFE_TRUST_DOMAIN / "
                "EXAMLOPS_SPIFFE_BUNDLE are unset, so no JWT-SVID can be verified"
            )
            _err("Startup check FAILED — workload identities mapped without a trust bundle")
        else:
            checks["workload_identity"] = "ok"

    if _LEGACY_MODE_RAW not in _LEGACY_MODES:
        checks["legacy_token"] = f"fail: CONTROL_PLANE_LEGACY_TOKEN={_LEGACY_MODE_RAW!r} (off)"
        _err(
            "Startup check FAILED — CONTROL_PLANE_LEGACY_TOKEN must be on, warn or off; treating "
            "%r as off",
            _LEGACY_MODE_RAW,
        )

    # Reported only when federation is configured: "off" is a valid deployment, and every value
    # in this dict other than "ok" turns /health degraded.
    iam_state = _iam_status()
    if iam_state != "off":
        checks["identity_federation"] = iam_state
    if iam_state.startswith("fail"):
        _err(
            "Startup check FAILED — identity federation trust file is invalid; "
            "federated tokens are refused: %s",
            iam_state,
        )

    try:
        coordinator = _get_coordinator()
        probe_key = f"control-plane:startup:{_instance_id}"
        if not coordinator.try_lock(probe_key, _instance_id, 5):
            raise RuntimeError("startup coordination probe was not acquired")
        coordinator.unlock(probe_key, _instance_id)
        checks["coordinator"] = "ok"
    except Exception as exc:
        _err("Startup check FAILED — coordinator: %s", exc)
        checks["coordinator"] = f"fail: {exc}"

    try:
        publisher = _selected_publisher()
        # Resolving proves the publisher is configured and its client library installed. Whether the
        # broker is *reachable* is asked once, at boot, with `check()`.
        #
        # On a recheck the relay's own last error answers it for free: it tries the broker every
        # cycle, so it knows sooner and costs nothing. Probing on every recheck instead made an
        # unrelated recovery wait for the broker's connect timeout — the combined-failure drill
        # measured 20 s to become ready again after the datastore returned, against 0.03 s when the
        # broker was up.
        if EVENT_RELAY_SECONDS > 0 and _relay_last_error:
            raise RuntimeError(_relay_last_error)
        probe = getattr(publisher, "check", None)
        if callable(probe) and not recheck:
            probe()
        checks["event_publisher"] = "ok"
    except Exception as exc:
        _err("Startup check FAILED — event publisher: %s", exc)
        checks["event_publisher"] = f"fail: {exc}"

    previous, _startup_checks = _startup_checks, checks
    _startup_checked_at = time.monotonic()
    ok = all(v == "ok" for v in checks.values())
    if not recheck:
        (logger.info if ok else logger.warning)("Startup checks: %s", checks)
    elif checks != previous:
        (logger.info if ok else logger.warning)("Startup checks re-evaluated: %s", checks)


def _recheck_failed_startup() -> None:
    """Re-run the startup checks while one of them is failing, at most every few seconds.

    They ran once at boot and never again, so a dependency that was briefly unavailable at that
    moment pinned the replica NotReady for its whole life — seen on a first `helm install`, where
    the schema bootstrap lost a race with another replica, failed once, and the table it looked
    for existed a second later. Checks that passed are not re-run: a probe must stay cheap.
    """
    if not _startup_checks:
        return
    failing = any(v != "ok" for v in _startup_checks.values())
    # A relay that cannot publish is the platform's own evidence that the backbone is gone, and it
    # arrives within a second. Without this the checks would never run again on a replica that
    # started healthy, and `/health` would keep calling the publisher "ok" while the outbox filled
    # up (the backbone chaos drill found exactly that).
    if not failing and not (EVENT_RELAY_SECONDS > 0 and _relay_last_error):
        return
    if time.monotonic() - _startup_checked_at < STARTUP_RECHECK_SECONDS:
        return
    with _STARTUP_RECHECK_LOCK:
        if time.monotonic() - _startup_checked_at >= STARTUP_RECHECK_SECONDS:
            _run_startup_checks(recheck=True)
    # Reported beside the checks, not among them: see health() on why the dispatch target decides
    # `status` but never readiness.
    dispatch = _dispatch_status(refresh=True)
    if dispatch["state"] != "ok":
        logger.warning(
            "Dispatch target %s is %s: %s",
            dispatch["deployment"],
            dispatch["state"],
            dispatch["detail"],
        )


# ─── Improvement 7 + 8: Poller state ─────────────────────────────────────────

_poller_last_ok_ts: float = 0.0
_poller_has_lease = False
_poller_coordination_error: str | None = None
_relay_last_result: dict[str, int] | None = None
_relay_last_error: str | None = None
_stop_event = threading.Event()
_background_threads: list[threading.Thread] = []


def _is_poller_stale() -> bool:
    if MODELZOO_POLL_SECONDS <= 0 or _poller_last_ok_ts == 0.0:
        return False
    interval = _modelzoo_settings().get("poll_interval_seconds", MODELZOO_POLL_SECONDS)
    return (time.time() - _poller_last_ok_ts) > 3 * interval


# ─── Improvement 16: Approval expiry ─────────────────────────────────────────


def _expire_old_approvals() -> int:
    """Mark pending approvals older than APPROVAL_EXPIRY_HOURS as 'expired'."""
    if APPROVAL_EXPIRY_HOURS <= 0:
        return 0
    cutoff = (datetime.utcnow() - timedelta(hours=APPROVAL_EXPIRY_HOURS)).isoformat()
    now = datetime.utcnow().isoformat()
    expired = 0
    conn = _get_db()
    try:
        cursor = conn.execute(
            "UPDATE pending_approvals SET status='expired', resolved_at=? "
            "WHERE status='pending' AND requested_at < ?",
            (now, cutoff),
        )
        expired = cursor.rowcount
        conn.commit()
    finally:
        conn.close()
    if expired:
        logger.info(
            "Expired %d stale pending approval(s) (>%dh old)", expired, APPROVAL_EXPIRY_HOURS
        )
        _metrics.record_approvals_expired(expired)
    return expired


# ─── Improvement 8 + 19: Lifespan ────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    _run_startup_checks()
    _expire_old_approvals()
    _get_registry()
    _stop_event.clear()
    _start_poller()
    _start_event_relay()
    _start_command_workers()
    _start_snapshot_projector()
    _start_drift_evaluator()
    _start_audit_maintenance()
    _start_feature_materializer()
    yield
    _stop_event.set()
    for thread in list(_background_threads):
        thread.join(timeout=5)
    _background_threads.clear()
    logger.info("Control plane shutting down")


# ─── Improvement 14: Request-ID middleware ────────────────────────────────────


class _RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        _request_id_var.set(req_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response


# ─── Improvement 18: Security headers middleware ──────────────────────────────


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "X-XSS-Protection": "1; mode=block",
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            }
        )
        return response


# ─── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="ExaMLOps Control Plane",
    version="0.13.0",
    description="Authoritative entry point for client-driven retraining and approval gates.",
    lifespan=lifespan,
)
app.add_middleware(_SecurityHeadersMiddleware)
app.add_middleware(_RequestIDMiddleware)
_problems.install(app)

# Allowed-hosts scoping (item 0.8): reject Host-header spoofing. Defaults permissive ("*")
# for local dev; set CONTROL_PLANE_ALLOWED_HOSTS to a comma-separated allow-list in prod
# (e.g. "control-plane.internal,cp.example.org").
_allowed_hosts = [
    h.strip() for h in os.getenv("CONTROL_PLANE_ALLOWED_HOSTS", "*").split(",") if h.strip()
]
if _allowed_hosts and _allowed_hosts != ["*"]:
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)


# ─── Auth & rate limiting ─────────────────────────────────────────────────────


def _federated_context(supplied: str, provider_hint: str | None) -> RequestContext | None:
    """Authenticate a data-center IdP token (ADR 0120), or ``None`` if it is not one.

    Only a JWT — or an opaque token addressed to a named provider (``X-ExaMLOps-IdP``) — is
    offered to the verifier, so a mistyped static token keeps its 403 instead of becoming a
    confusing federation error. A token that *is* federated but fails verification is a 401
    (RFC 6750 ``invalid_token``); a valid token whose IdP grants no ExaMLOps role is a 403.
    Tenant comes from the issuer's binding in the trust file, never from the request.
    """
    if _iam_status() != "ok":
        return None
    from examlops import iam

    if not (iam.looks_like_jwt(supplied) or provider_hint):
        return None
    try:
        principal = iam.verify_access_token(supplied, provider_hint=provider_hint)
    except iam.AuthenticationError as exc:
        logger.warning("Federated token rejected: %s", exc.reason)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"Invalid bearer token: {exc.reason}",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc
    if principal.role is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Authenticated as {principal.actor}, but your identity provider grants no ExaMLOps role",
        )
    scopes = {"read", "write"} if principal.has_role("operator") else {"read"}
    return RequestContext(
        principal=principal.id,
        tenant=principal.tenant,
        scopes=frozenset(scopes),
        identity=principal,
        credential="federated",
    )


def _request_context(
    authorization: str | None = Header(default=None),
    request: Request = None,  # type: ignore[assignment]  # injected by FastAPI; None when called directly
) -> RequestContext:
    """Authenticate one configured bearer credential and return its trusted identity context.

    Static credentials (``CONTROL_PLANE_TOKEN`` / ``CONTROL_PLANE_CREDENTIALS_JSON``) are checked
    first; otherwise a token from a trusted data-center IdP is verified (ADR 0120).
    """
    if _credential_config_error is not None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Control plane credential map is malformed",
        )
    if not _auth_is_usable():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Control plane authentication is not configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    supplied = authorization.removeprefix("Bearer ").strip()
    matched: RequestContext | None = None
    # Scan every configured secret so credential selection does not expose a direct dictionary
    # lookup timing oracle. Never derive identity or tenancy from request-controlled headers/body.
    for token, context in _structured_credentials.items():
        if _hmac.compare_digest(supplied, token):
            matched = context
    if _token_is_usable() and _hmac.compare_digest(supplied, CONTROL_PLANE_TOKEN):
        matched = RequestContext(
            "legacy", "default", frozenset({"read", "write"}), is_legacy=True, credential="legacy"
        )
        _note_legacy_use(request)
    elif (
        matched is None
        and LEGACY_TOKEN_MODE == "off"
        and CONTROL_PLANE_TOKEN
        and _hmac.compare_digest(supplied, CONTROL_PLANE_TOKEN)
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The shared legacy token is disabled (CONTROL_PLANE_LEGACY_TOKEN=off); use this "
            "service's own credential",
        )
    if matched is None and _workload_identities:
        from examlops import workload_identity  # noqa: PLC0415

        if workload_identity.looks_like_svid(supplied):
            matched = _workload_context(supplied)  # a SPIFFE workload (ADR 0125)
    if matched is None:
        # `X-ExaMLOps-IdP` names the provider for an opaque token. Read from the request rather
        # than declared as a parameter so it stays out of every route's API contract.
        hint = request.headers.get("x-examlops-idp") if isinstance(request, Request) else None
        matched = _federated_context(supplied, hint)
    if matched is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid bearer token")
    _metrics.record_authentication(
        "federated" if matched.credential == "federated" else matched.principal, matched.credential
    )
    return matched


def _require_scope(required: str) -> Callable[..., RequestContext]:
    def dependency(
        request: Request, context: RequestContext = Depends(_request_context)
    ) -> RequestContext:
        if required not in context.scopes:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing {required!r} scope")
        if context.identity is not None:
            # Federated caller: the data center's own PDP may veto (ADR 0120 — tenant isolation,
            # then platform policy, then the center's AuthZEN/OPA decision, deny-overrides).
            from examlops import iam

            decision = iam.authorize(
                context.identity,
                f"api.{required}",
                {
                    "type": "control_plane",
                    "id": request.url.path,
                    "method": request.method,
                    "tenant": context.tenant,
                },
                local_allowed=True,
            )
            if not decision.allowed:
                raise HTTPException(status.HTTP_403_FORBIDDEN, f"Denied: {decision.reason}")
        return context

    return dependency


_require_read_context = _require_scope("read")
_require_write_context = _require_scope("write")


def _require_action(action: str) -> Callable[..., RequestContext]:
    """A mutation that ``write`` or the narrower ``action`` scope may perform (plan P3.2)."""
    assert action in ACTION_SCOPES, action
    by_write = _require_scope("write")
    by_action = _require_scope(action)

    def dependency(
        request: Request, context: RequestContext = Depends(_request_context)
    ) -> RequestContext:
        if "write" in context.scopes:
            return by_write(request, context)
        if action in context.scopes:
            return by_action(request, context)
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing 'write' or {action!r} scope")

    return dependency


def _require_token(authorization: str | None = Header(default=None)) -> RequestContext:
    """Compatibility alias for older direct callers; write routes use scoped dependencies."""
    return _request_context(authorization)


def _check_rate_limit(
    # Authenticated, not scope-checked: each route's own dependency decides which scope it needs
    # (`write`, or an action scope such as `retrain`), and a `write` requirement here would refuse
    # every narrow credential before its scope was ever looked at (plan P3.2).
    context: RequestContext = Depends(_request_context),
) -> None:
    if RETRAIN_RATE_LIMIT_PER_MIN <= 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded",
            headers={"Retry-After": "60"},
        )
    try:
        allowed = _get_coordinator().allow(
            f"control-plane:writes:{context.tenant}", RETRAIN_RATE_LIMIT_PER_MIN, 60.0
        )
    except Exception as exc:
        logger.error("Shared rate limiter unavailable: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Shared rate limiter unavailable",
        ) from exc
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded",
            headers={"Retry-After": "60"},
        )


# ─── ModelZoo webhook auth ────────────────────────────────────────────────────


async def _read_webhook_body(request: Request) -> bytes:
    """The request body, refusing anything over ``CONTROL_PLANE_WEBHOOK_MAX_BYTES`` (413).

    Checked against the declared length first and against the bytes actually streamed, so a lying
    or absent Content-Length cannot make the service buffer an unbounded body before auth runs.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > WEBHOOK_MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Webhook body too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > WEBHOOK_MAX_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Webhook body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _verify_gitlab_token(x_gitlab_token: str | None) -> None:
    if not MODELZOO_WEBHOOK_SECRET:
        raise HTTPException(503, "MODELZOO_WEBHOOK_SECRET not configured")
    # Constant-time compare (matches the GitHub HMAC path).
    if not x_gitlab_token or not _hmac.compare_digest(x_gitlab_token, MODELZOO_WEBHOOK_SECRET):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid X-Gitlab-Token")


def _verify_github_signature(raw_body: bytes, x_hub_signature_256: str | None) -> None:
    if not MODELZOO_WEBHOOK_SECRET:
        raise HTTPException(503, "MODELZOO_WEBHOOK_SECRET not configured")
    if not x_hub_signature_256:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-Hub-Signature-256")
    expected = (
        "sha256="
        + _hmac.new(MODELZOO_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    )
    if not _hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid X-Hub-Signature-256")


def _record_push_event(
    commit_sha: str, branch: str, pushed_by: str, raw_payload: str
) -> dict[str, Any]:
    now = datetime.utcnow().isoformat()
    registry = _get_registry()
    event_id: int = 0

    conn = _get_db()
    try:
        conn.execute(begin_immediate("modelzoo"))
        # A redelivered or replayed webhook names a commit already recorded (by the webhook or
        # by the poller). It used to insert another event row and re-fire the CI pipeline and
        # every auto-retrain; now it is acknowledged and does nothing (plan P0.8 / finding S8).
        seen = conn.execute(
            "SELECT id FROM modelzoo_events WHERE commit_sha = ? LIMIT 1", (commit_sha,)
        ).fetchone()
        if seen:
            conn.commit()
            return {
                "event_id": int(seen[0]),
                "duplicate": True,
                "models_marked_stale": 0,
                "retrain_triggered": False,
                "ci_pipeline_triggered": False,
            }
        cursor = conn.execute(
            "INSERT INTO modelzoo_events (commit_sha, branch, pushed_by, timestamp, source, raw_payload) "
            "VALUES (?, ?, ?, ?, 'webhook', ?)",
            (commit_sha, branch, pushed_by, now, raw_payload),
        )
        event_id = int(cursor.lastrowid or 0)
        for model_id in registry:
            conn.execute(
                "INSERT INTO model_freshness (model_id, latest_modelzoo_commit, is_stale, stale_since) "
                "VALUES (?, ?, 1, ?) ON CONFLICT(model_id) DO UPDATE SET "
                "latest_modelzoo_commit=excluded.latest_modelzoo_commit, is_stale=1, stale_since=excluded.stale_since",
                (model_id, commit_sha, now),
            )
        conn.commit()
    finally:
        conn.close()

    retrain_triggered = False
    if _modelzoo_settings().get("auto_retrain") and registry:
        for model_id, datasets in registry.items():
            if datasets:
                try:
                    _auto_retrain_model(model_id, datasets[0], commit_sha)
                    retrain_triggered = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Auto-retrain failed for %s: %s", model_id, exc)

    ci_pipeline_triggered = _trigger_ci_pipeline(commit_sha)
    return {
        "event_id": event_id,
        "models_marked_stale": len(registry),
        "retrain_triggered": retrain_triggered,
        "ci_pipeline_triggered": ci_pipeline_triggered,
    }


def _auto_retrain_model(model_id: str, dataset_name: str, commit_sha: str) -> None:
    parameters = {
        "model_name": model_id,
        "dataset_cls_name": dataset_name,
        "is_dummy": False,
        "backend_name": None,
    }
    command_key = f"modelzoo:{commit_sha}:{model_id}:{dataset_name}"
    claim = _claim_command(
        command_key,
        "modelzoo_retrain",
        parameters,
        actor="system:modelzoo",
        tenant="default",
    )
    if claim.outcome != "claimed":
        logger.info("ModelZoo retrain already %s for model=%s", claim.outcome, model_id)
        return

    gateway = _get_gateway()
    try:
        flow_run_id = _dispatch_flow_run(gateway, parameters, command_key)
        _complete_command(
            command_key,
            {"flow_run_id": flow_run_id, "model_id": model_id},
            event_topic="modelzoo.retrain_scheduled",
            event_payload={
                "model_id": model_id,
                "commit_sha": commit_sha,
                "flow_run_id": flow_run_id,
            },
            attempt=claim.attempt or 0,
            freshness=(model_id, commit_sha),
            audit=(
                "retrain_dispatched",
                model_id,
                {"dataset": dataset_name, "flow_run_id": flow_run_id, "commit_sha": commit_sha},
            ),
        )
    except Exception as exc:
        _fail_command(command_key, exc, attempt=claim.attempt or 0)
        raise
    logger.info("Auto-retrain triggered model=%s flow_run_id=%s", model_id, flow_run_id)


def _run_poll_cycle() -> dict[str, Any]:
    if not GITLAB_TOKEN or not GITLAB_PROJECT_ID:
        return {}

    import urllib.parse as _uparse  # noqa: PLC0415
    import urllib.request as _ureq  # noqa: PLC0415

    project_id_enc = _uparse.quote(str(GITLAB_PROJECT_ID), safe="")
    url = (
        f"{GITLAB_URL}/api/v4/projects/{project_id_enc}/repository/commits"
        f"?ref_name={_uparse.quote(MODELZOO_WATCH_BRANCH, safe='')}&per_page=1"
    )
    req = _ureq.Request(url, headers={"PRIVATE-TOKEN": GITLAB_TOKEN})
    try:
        with _ureq.urlopen(req, timeout=10.0) as resp:  # noqa: S310
            commits = json.loads(resp.read().decode())
    except Exception as exc:
        # A failed GitLab fetch is an ERROR, not "no new commits". Return a
        # distinct marker so the poller treats the cycle as failed (health goes
        # stale) and `exa modelzoo sync` surfaces the reason instead of silently
        # reporting "up-to-date".
        logger.warning("ModelZoo poll failed: %s", exc)
        return {"error": str(exc)}

    if not commits:
        return {}

    latest_sha: str = commits[0]["id"]
    pushed_by: str = commits[0].get("author_name", "unknown")
    committed_at: str = commits[0].get("committed_date", datetime.utcnow().isoformat())

    now = datetime.utcnow().isoformat()
    registry = _get_registry()
    event_id: int = 0
    conn = _get_db()
    try:
        # Same scope as the webhook path: both check-then-insert on modelzoo_events.
        conn.execute(begin_immediate("modelzoo"))
        existing = conn.execute(
            "SELECT id FROM modelzoo_events WHERE commit_sha = ?", (latest_sha,)
        ).fetchone()
        if existing:
            conn.commit()
            return {}
        cursor = conn.execute(
            "INSERT INTO modelzoo_events (commit_sha, branch, pushed_by, timestamp, source) VALUES (?, ?, ?, ?, 'poll')",
            (latest_sha, MODELZOO_WATCH_BRANCH, pushed_by, committed_at),
        )
        event_id = int(cursor.lastrowid or 0)
        for model_id in registry:
            conn.execute(
                "INSERT INTO model_freshness (model_id, latest_modelzoo_commit, is_stale, stale_since) "
                "VALUES (?, ?, 1, ?) ON CONFLICT(model_id) DO UPDATE SET "
                "latest_modelzoo_commit=excluded.latest_modelzoo_commit, is_stale=1, stale_since=excluded.stale_since",
                (model_id, latest_sha, now),
            )
        conn.commit()
    finally:
        conn.close()

    logger.info("ModelZoo poll: new commit %s by %s", latest_sha[:8], pushed_by)

    if _modelzoo_settings().get("auto_retrain") and registry:
        for model_id, datasets in registry.items():
            if datasets:
                try:
                    _auto_retrain_model(model_id, datasets[0], latest_sha)
                except Exception as exc:
                    logger.warning("Auto-retrain failed for %s: %s", model_id, exc)

    _trigger_ci_pipeline(latest_sha)
    return {"new": True, "commit_sha": latest_sha, "event_id": event_id}


def _run_leased_poll_cycle(holder: str) -> bool:
    """Renew/acquire poller leadership and run one cycle only for the lease owner."""
    global _poller_coordination_error, _poller_has_lease, _poller_last_ok_ts
    interval = max(1, int(_modelzoo_settings().get("poll_interval_seconds", MODELZOO_POLL_SECONDS)))
    lease_seconds = max(POLLER_LEASE_SECONDS, interval * 3)
    try:
        _poller_has_lease = _get_coordinator().try_lock(
            "control-plane:modelzoo-poller", holder, lease_seconds
        )
        _poller_coordination_error = None
    except Exception as exc:
        _poller_has_lease = False
        _poller_coordination_error = str(exc)
        logger.warning("ModelZoo poller coordination failed: %s", exc)
        return False
    if not _poller_has_lease:
        return False
    try:
        result = _run_poll_cycle()
        _expire_old_approvals()
        # Only count the cycle as healthy if the GitLab fetch did not error. A network/DNS
        # failure must surface as a stale poller rather than masquerading as a successful no-op.
        if result.get("error"):
            logger.warning("ModelZoo poll cycle failed: %s", result["error"])
        else:
            _poller_last_ok_ts = time.time()
    except Exception as exc:
        logger.warning("Poll cycle error: %s", exc)
    return True


def _start_poller() -> None:
    if MODELZOO_POLL_SECONDS <= 0:
        logger.info("ModelZoo poller disabled (MODELZOO_POLL_SECONDS=0)")
        return

    def _loop() -> None:
        global _poller_has_lease
        lock_key = "control-plane:modelzoo-poller"
        holder = f"{_instance_id}:poller"
        logger.info(
            "ModelZoo poller started (interval=%ds)",
            _modelzoo_settings().get("poll_interval_seconds", MODELZOO_POLL_SECONDS),
        )
        try:
            while not _stop_event.wait(
                timeout=_modelzoo_settings().get("poll_interval_seconds", MODELZOO_POLL_SECONDS)
            ):
                _run_leased_poll_cycle(holder)
        finally:
            try:
                _get_coordinator().unlock(lock_key, holder)
            except Exception as exc:
                logger.warning("Could not release ModelZoo poller lease: %s", exc)
            _poller_has_lease = False
            logger.info("ModelZoo poller stopped")

    thread = threading.Thread(target=_loop, daemon=True, name="modelzoo-poller")
    _background_threads.append(thread)
    thread.start()


def _relay_outbox_once() -> dict[str, int]:
    """Relay the control-plane outbox through the configured publisher."""
    # Resolve eagerly so a bad publisher selection fails before touching durable claims. The
    # shared relay resolves the same cached instance and reads PLATFORM_DB, aligned above with the
    # control-plane SQLite database (or the common Postgres schema).
    _selected_publisher()
    return _shared_relay_once(EVENT_RELAY_BATCH_SIZE)


def _relay_error_from(result: dict[str, Any]) -> str | None:
    """Why the backbone is not delivering, in one line for `/health`, or ``None`` when it is.

    An unreachable broker ends a relay batch (``examlops.events.relay_once``), so ``failed`` is 0
    while nothing at all can be delivered. Reading only ``failed`` therefore published
    ``status: ok`` through a real outage: the backbone chaos drill's combined-failure section
    measured 36 seconds of it, with six events sitting in the outbox the whole time.
    """
    # Presence, not truthiness: the relay reports the reason it was given, and some clients raise
    # an exception with no message at all (see `examlops.events.describe`).
    if "unavailable" in result:
        unavailable = result.get("unavailable") or "no reason given"
        deferred = result.get("deferred", 0)
        return (
            f"event backbone unavailable ({unavailable}); {deferred} event(s) waiting in the outbox"
        )
    failed = result.get("failed", 0)
    return f"{failed} event(s) failed to publish" if failed else None


def _refresh_backbone_metrics() -> None:
    """Consumer lag and dead-letter depth from JetStream (P2.7); only the `nats` publisher has any.

    A failed read keeps the last values rather than publishing zeros — zero lag is the reading
    that means "healthy", and it must not be what an unreachable broker produces.
    """
    if os.getenv("EXAMLOPS_EVENT_PUBLISHER", "log").strip().lower() != "nats":
        return
    try:
        from examlops.events import nats_backend

        _metrics.set_backbone(nats_backend.shared().backbone_stats())
    except Exception as exc:  # noqa: BLE001 - metrics must never stop the relay
        # Counted, not only logged: the gauges above now hold values of unknown age, and the
        # alerts built on them cannot tell. `EventBackboneMetricsUnreadable` is that signal.
        _metrics.record_backbone_read_error()
        logger.warning("Could not read event backbone statistics: %s", exc)


_snapshot_projector: Any = None


def _start_snapshot_projector() -> None:
    global _snapshot_projector
    if SNAPSHOT_SECONDS <= 0:
        logger.info("Serving snapshot projector disabled (CONTROL_PLANE_SNAPSHOT_SECONDS=0)")
        _snapshot_projector = None
        return
    from cplane.projector import SnapshotProjector

    _snapshot_projector = SnapshotProjector(
        coordinator=_get_coordinator,
        holder=f"{_instance_id}:snapshot",
        interval=SNAPSHOT_SECONDS,
        lease_seconds=max(POLLER_LEASE_SECONDS, SNAPSHOT_TICK_SECONDS * 10),
        metrics=_metrics,
    )
    thread = threading.Thread(
        target=_snapshot_projector.run,
        args=(_stop_event, SNAPSHOT_TICK_SECONDS),
        daemon=True,
        name="serving-snapshot",
    )
    _background_threads.append(thread)
    thread.start()


def _evaluate_drift_once() -> list[dict[str, Any]]:
    """Score every model; record and announce status changes. Never raises: counted instead."""
    from examlops import drift_status  # noqa: PLC0415

    try:
        changes = drift_status.evaluate(actor="control-plane")
    except Exception as exc:  # noqa: BLE001 - one bad evaluation must not end the loop
        _metrics.record_drift_evaluation_error()
        logger.warning("Drift evaluation failed: %s", exc)
        return []
    for change in changes:
        _metrics.record_drift_status_change(change["status"])
        logger.info(
            "Drift status of %s: %s -> %s (z=%s)",
            change["model"],
            change["previous"],
            change["status"],
            change["z_score"],
        )
    return changes


def _start_drift_evaluator() -> None:
    if DRIFT_EVAL_SECONDS <= 0:
        logger.info("Drift evaluator disabled (CONTROL_PLANE_DRIFT_EVAL_SECONDS=0)")
        return

    def _loop() -> None:
        # Every replica may run it: the change is recorded and announced under one write lock
        # (examlops.drift_status), so a change is announced once whichever replica sees it first.
        while not _stop_event.wait(timeout=DRIFT_EVAL_SECONDS):
            _evaluate_drift_once()

    thread = threading.Thread(target=_loop, daemon=True, name="drift-evaluator")
    _background_threads.append(thread)
    thread.start()


def _audit_maintenance_once() -> dict[str, Any]:
    """One scheduled audit-maintenance cycle (ADR 0028). Never raises: counted instead."""
    from examlops import audit_maintenance  # noqa: PLC0415

    try:
        result = audit_maintenance.run_cycle()
    except Exception as exc:  # noqa: BLE001 - one bad cycle must not end the loop
        logger.warning("Audit maintenance cycle failed: %s", exc)
        result = {"status": "error", "reason": str(exc)}
    _metrics.record_audit_maintenance(result, time.time())
    return result


def _start_audit_maintenance() -> None:
    from examlops import audit_maintenance  # noqa: PLC0415

    every = audit_maintenance.interval_seconds()
    if every <= 0:
        logger.info("Audit maintenance disabled (EXAMLOPS_AUDIT_MAINTENANCE_SECONDS=0)")
        return

    def _loop() -> None:
        # Every replica may run it: a cycle holds the cluster-wide `audit-maintenance` lease, so
        # one replica does the work and the others record `skipped`.
        while not _stop_event.wait(timeout=every):
            _audit_maintenance_once()

    thread = threading.Thread(target=_loop, daemon=True, name="audit-maintenance")
    _background_threads.append(thread)
    thread.start()


def _materialize_features_once() -> dict[str, Any] | None:
    """One scheduled feature-materialization cycle (ADR 0017 clause 4). Never raises."""
    try:
        from examlops.feature_store.scheduler import run_materialization_cycle

        result = run_materialization_cycle(actor="control-plane")
    except Exception as exc:  # noqa: BLE001 - one bad cycle must not end the loop
        _metrics.record_feature_materialization_cycle({"failed": [{"error": str(exc)}]})
        logger.warning("Feature materialization cycle failed: %s", exc)
        return None
    _metrics.record_feature_materialization_cycle(result)
    for m in result["materialized"]:
        logger.info("Materialized feature view %s (%s rows)", m["view"], m["rows"])
    for f in result["failed"]:
        logger.warning("Feature view %s failed to materialize: %s", f["view"], f["error"])
    for f in result.get("mirror_failed") or []:
        logger.warning("Feature view %s serving-tier mirror failed: %s", f["view"], f["error"])
    return result


def _start_feature_materializer() -> None:
    if FEATURE_MATERIALIZE_SECONDS <= 0:
        logger.info("Feature materializer disabled (CONTROL_PLANE_FEATURE_MATERIALIZE_SECONDS=0)")
        return

    def _loop() -> None:
        # Every replica may tick: a per-view coordinator lock makes one of them do the work.
        while not _stop_event.wait(timeout=FEATURE_MATERIALIZE_SECONDS):
            _materialize_features_once()

    thread = threading.Thread(target=_loop, daemon=True, name="feature-materializer")
    _background_threads.append(thread)
    thread.start()


def _start_event_relay() -> None:
    if EVENT_RELAY_SECONDS <= 0:
        logger.info("Event relay disabled (CONTROL_PLANE_EVENT_RELAY_SECONDS=0)")
        return

    def _loop() -> None:
        global _relay_last_error, _relay_last_result
        logger.info("Event relay started (interval=%.1fs)", EVENT_RELAY_SECONDS)
        next_backbone_read = 0.0
        while not _stop_event.wait(timeout=EVENT_RELAY_SECONDS):
            try:
                _relay_last_result = _relay_outbox_once()
                _metrics.record_relay(_relay_last_result)
                _relay_last_error = _relay_error_from(_relay_last_result)
            except Exception as exc:
                _metrics.record_relay_cycle_error()
                _relay_last_error = str(exc)
                logger.warning("Event relay cycle failed: %s", exc)
            if time.monotonic() >= next_backbone_read:
                next_backbone_read = time.monotonic() + EVENT_BACKBONE_STATS_SECONDS
                _refresh_backbone_metrics()
        logger.info("Event relay stopped")

    thread = threading.Thread(target=_loop, daemon=True, name="event-outbox-relay")
    _background_threads.append(thread)
    thread.start()


def _trigger_ci_pipeline(commit_sha: str) -> bool:
    """Trigger the ai-production CI pipeline via GitLab pipeline trigger API.

    Called after every confirmed-new modelzoo commit (webhook or poll) so that
    test:modelzoo always runs against the latest upstream code automatically.
    Silently skips when AI_PROD_GITLAB_PROJECT_ID / AI_PROD_PIPELINE_TRIGGER_TOKEN
    are not configured.
    """
    if not AI_PROD_PROJECT_ID or not AI_PROD_PIPELINE_TOKEN:
        return False
    import urllib.parse as _uparse  # noqa: PLC0415
    import urllib.request as _ureq  # noqa: PLC0415

    try:
        project_id_enc = _uparse.quote(str(AI_PROD_PROJECT_ID), safe="")
        url = f"{GITLAB_URL}/api/v4/projects/{project_id_enc}/trigger/pipeline"
        data = _uparse.urlencode(
            {
                "token": AI_PROD_PIPELINE_TOKEN,
                "ref": "main",
                "variables[MODELZOO_COMMIT]": commit_sha,
            }
        ).encode()
        req = _ureq.Request(url, data=data, method="POST")
        with _ureq.urlopen(req, timeout=10) as resp:  # noqa: S310
            result = json.loads(resp.read().decode())
        logger.info(
            "CI pipeline triggered id=%s for modelzoo commit %s",
            result.get("id"),
            commit_sha[:8],
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("CI pipeline trigger failed: %s", exc)
        return False


# ─── Registry helpers ─────────────────────────────────────────────────────────


def _load_registry() -> dict[str, list[str]]:
    import yaml  # noqa: PLC0415
    from model_meta import resolve_models_dir  # noqa: PLC0415

    result: dict[str, list[str]] = {}
    try:
        # ADR 0094: model YAML lives in the active use-case pack, not the removed
        # pipelines/models path. Reuse the single resolver so the registry never re-empties.
        models_dir = str(resolve_models_dir())
        if not os.path.isdir(models_dir):
            logger.warning("Models dir not found: %s", models_dir)
            return {}
        for fname in sorted(os.listdir(models_dir)):
            if not fname.endswith((".yaml", ".yml")):
                continue
            fpath = os.path.join(models_dir, fname)
            try:
                with open(fpath) as fh:
                    cfg = yaml.safe_load(fh)
                if not cfg or not cfg.get("enabled", True):
                    continue
                name = cfg.get("name")
                if not name:
                    continue
                result[name] = [d["name"] for d in cfg.get("datasets", []) if d.get("name")]
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping %s: %s", fname, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("YAML registry scan failed: %s", exc)
    return result


_gateway: PrefectGateway | None = None


def _dispatch_flow_run(gateway: Any, parameters: dict[str, Any], idempotency_key: str) -> str:
    """Resolve the dispatch deployment and create the flow run inside ONE deadline (plan P1.5)."""
    with _dispatch_budget():
        deployment_id = gateway.find_deployment_id(PREFECT_DEPLOYMENT_NAME)
        return gateway.create_flow_run(deployment_id, parameters, idempotency_key=idempotency_key)


def _get_gateway() -> PrefectGateway:
    global _gateway
    if _gateway is None:
        # The app's PREFECT_API_URL, not the gateway module's own default, so configuration (and
        # a test that points it somewhere) has one source.
        _gateway = PrefectGateway(api_url=PREFECT_API_URL)
    return _gateway


# ─── Retrain dedup (improvement 5) ───────────────────────────────────────────

_inflight_retrains: set[str] = set()
_RETRAIN_LOCK = threading.Lock()


def _retrain_key(model_name: str, dataset_name: str) -> str:
    return f"{model_name}:{dataset_name}"


def _release_retrain_guards(local_key: str, coordinator: Any, lock_key: str, holder: str) -> None:
    with _RETRAIN_LOCK:
        _inflight_retrains.discard(local_key)
    try:
        coordinator.unlock(lock_key, holder)
    except Exception as exc:
        # The lease expires even if the backend is temporarily unavailable during release.
        logger.warning("Could not release retrain lease %s: %s", lock_key, exc)


@dataclass(frozen=True)
class _CommandClaim:
    outcome: str  # claimed | busy | succeeded | capacity
    response: dict[str, Any] | None = None
    attempt: int | None = None


ADMISSION_RETRY_AFTER_SECONDS = max(1, int(os.getenv("CONTROL_PLANE_ADMISSION_RETRY_AFTER", "30")))

# Plan P0.8 / finding S7: the principal that requested a change may not approve it. The shared
# legacy token is exempt because it cannot tell requester from approver — every holder is
# `legacy` — which /health reports rather than hides. Use structured or federated credentials to
# get an enforced gate.
SEPARATION_OF_DUTIES = os.getenv(
    "CONTROL_PLANE_SEPARATION_OF_DUTIES", "true"
).strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
WEBHOOK_MAX_BYTES = max(
    1024, int(os.getenv("CONTROL_PLANE_WEBHOOK_MAX_BYTES", str(5 * 1024 * 1024)))
)


def _audit(
    conn: Any, actor: str, action: str, target: str, details: dict[str, Any], tenant: str
) -> None:
    """Record a governance decision in the hash-chained audit log, inside the caller's transaction.

    The approval gate used to log decisions to stdout only, so `exa audit` could not show who
    approved, rejected or retrained what. Appending on the caller's connection makes the record
    atomic with the decision: both commit or neither does.
    """
    append_audit_event(conn, "control-plane", actor, action, target, details=details, tenant=tenant)


def _release_abandoned_admissions(conn: Any, stale_before: str) -> None:
    """Free admission slots that no live worker holds. Runs inside the claim transaction.

    Two kinds of row outlive the request that created them:

    * ``running`` rows whose command is still ``dispatching`` past its lease — the worker
      crashed between claim and completion. They held a slot forever.
    * ``queued`` rows left by releases before the B1 fix, which refused a request but kept its
      row queued. Nothing ever ran them.

    Both are restricted to rows a control-plane command owns: on Postgres ``admission_queue`` is
    shared with the platform-wide ``exa admission`` queue, whose queued items are real work that
    this service must never touch.
    """
    conn.execute(
        "UPDATE admission_queue SET state='failed', finished_at=CURRENT_TIMESTAMP, "
        "reason='dispatch lease expired' WHERE state='running' AND id IN ("
        "SELECT admission_id FROM control_plane_commands "
        "WHERE state='dispatching' AND updated_at <= ? AND admission_id IS NOT NULL)",
        (stale_before,),
    )
    conn.execute(
        "UPDATE admission_queue SET state='deferred', reason='abandoned by a refused caller' "
        "WHERE state='queued' AND id IN ("
        "SELECT admission_id FROM control_plane_commands WHERE admission_id IS NOT NULL)"
    )


def _admission_refused() -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "Admission capacity exhausted for this tenant; retry with the same Idempotency-Key",
        headers={"Retry-After": str(ADMISSION_RETRY_AFTER_SECONDS)},
    )


def _command_payload(kind: str, parameters: dict[str, Any]) -> tuple[str, str]:
    payload = json.dumps(
        {"kind": kind, "parameters": parameters}, sort_keys=True, separators=(",", ":")
    )
    return payload, hashlib.sha256(payload.encode()).hexdigest()


def _claim_command(
    command_key: str,
    kind: str,
    parameters: dict[str, Any],
    *,
    approval_id: str | None = None,
    actor: str = "system",
    tenant: str = "default",
) -> _CommandClaim:
    """Create or atomically claim a durable Prefect command.

    A completed key replays its stored response. A key reused for different input is rejected.
    Dispatch claims have a lease, so a process crash cannot strand a command indefinitely.
    """
    payload, request_hash = _command_payload(kind, parameters)
    now = datetime.utcnow().isoformat()
    stale_before = (datetime.utcnow() - timedelta(seconds=COMMAND_LEASE_SECONDS)).isoformat()
    conn = _get_db()
    try:
        # Serialize the read-modify-write claim across processes. The shared Postgres adapter
        # translates this to a transaction-scoped advisory lock.
        conn.execute(begin_immediate("admission"))
        conn.execute(
            "INSERT OR IGNORE INTO control_plane_commands "
            "(command_key, kind, request_hash, payload, state, approval_id, actor, tenant, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
            (command_key, kind, request_hash, payload, approval_id, actor, tenant, now, now),
        )
        row = conn.execute(
            "SELECT request_hash, state, response, admission_id, attempts, actor, tenant "
            "FROM control_plane_commands "
            "WHERE command_key = ?",
            (command_key,),
        ).fetchone()
        if row is None:  # pragma: no cover - the insert/select are one transaction
            raise RuntimeError("durable command disappeared after insert")
        if row[0] != request_hash:
            raise HTTPException(409, "Idempotency key was already used for different input")
        if row[5] != actor or row[6] != tenant:
            raise HTTPException(403, "Command belongs to a different principal or tenant")
        if row[1] == "succeeded":
            return _CommandClaim("succeeded", json.loads(row[2]))

        admission_id = row[3]
        if admission_id is None:
            cursor = conn.execute(
                "INSERT INTO admission_queue "
                "(tenant, kind, payload, state) VALUES (?, ?, ?, 'queued')",
                (tenant, kind, payload),
            )
            admission_id = int(cursor.lastrowid or 0)
            conn.execute(
                "UPDATE control_plane_commands SET admission_id = ? WHERE command_key = ?",
                (admission_id, command_key),
            )

        reclaimable = row[1] in ("pending", "failed") or (
            row[1] == "dispatching"
            and conn.execute(
                "SELECT 1 FROM control_plane_commands WHERE command_key=? AND updated_at <= ?",
                (command_key, stale_before),
            ).fetchone()
            is not None
        )
        if not reclaimable:
            conn.commit()
            return _CommandClaim("busy")

        _release_abandoned_admissions(conn, stale_before)

        # The HTTP caller is the worker for this synchronous API: it is admitted now, while it
        # waits, or it is refused and leaves. Only the global and per-tenant caps decide. There
        # is deliberately no "must be the oldest queued row" rule — with synchronous callers
        # the oldest row belongs to a request that was already answered, and FIFO-by-head
        # turned one refusal into a permanent wedge (finding B1).
        conn.execute(
            "UPDATE admission_queue SET state='queued', started_at=NULL, finished_at=NULL, "
            "reason=NULL WHERE id=?",
            (admission_id,),
        )
        running_total = conn.execute(
            "SELECT COUNT(*) FROM admission_queue WHERE state='running'"
        ).fetchone()[0]
        running_tenant = conn.execute(
            "SELECT COUNT(*) FROM admission_queue WHERE state='running' AND tenant=?",
            (tenant,),
        ).fetchone()[0]
        if (
            int(running_total) >= admission_max_running()
            or int(running_tenant) >= admission_per_tenant_cap()
        ):
            # Refused: the caller goes away with a retryable answer, so its row must not stay
            # ``queued`` — nothing would ever run it. A retry with the same idempotency key
            # re-queues this same row.
            conn.execute(
                "UPDATE admission_queue SET state='deferred', reason='capacity' WHERE id=?",
                (admission_id,),
            )
            conn.commit()
            return _CommandClaim("capacity")

        claimed = conn.execute(
            "UPDATE control_plane_commands "
            "SET state='dispatching', attempts=attempts+1, last_error=NULL, updated_at=? "
            "WHERE command_key=? AND "
            "(state IN ('pending', 'failed') OR (state='dispatching' AND updated_at <= ?))",
            (now, command_key, stale_before),
        ).rowcount
        if claimed:
            conn.execute(
                "UPDATE admission_queue SET state='running', started_at=CURRENT_TIMESTAMP, "
                "finished_at=NULL, reason=NULL WHERE id=?",
                (admission_id,),
            )
            if approval_id is not None:
                approval_claimed = conn.execute(
                    "UPDATE pending_approvals SET status='approving' "
                    "WHERE id=? AND tenant=? AND status IN ('pending', 'approving')",
                    (approval_id, tenant),
                ).rowcount
                if not approval_claimed:
                    raise HTTPException(409, "Approval is no longer pending")
        conn.commit()
        if not claimed:
            return _CommandClaim("busy")
        return _CommandClaim("claimed", attempt=int(row[4]) + 1)
    finally:
        conn.close()


def _complete_command(
    command_key: str,
    response: dict[str, Any],
    *,
    event_topic: str,
    event_payload: dict[str, Any],
    attempt: int,
    approval_id: str | None = None,
    freshness: tuple[str, str] | None = None,
    audit: tuple[str, str, dict[str, Any]] | None = None,
) -> None:
    """Commit command success, queue completion, approval state, outbox event and audit atomically.

    ``audit`` is ``(action, target, details)``; the actor and tenant come from the command row.
    """
    now = datetime.utcnow().isoformat()
    conn = _get_db()
    try:
        conn.execute(begin_immediate("admission"))
        row = conn.execute(
            "SELECT admission_id, actor, tenant FROM control_plane_commands WHERE command_key=?",
            (command_key,),
        ).fetchone()
        if row is None:  # pragma: no cover - command keys are created before dispatch
            raise RuntimeError("durable command disappeared before completion")
        actor, tenant = row[1], row[2]
        completed = conn.execute(
            "UPDATE control_plane_commands SET state='succeeded', response=?, prefect_run_id=?, "
            "last_error=NULL, updated_at=? "
            "WHERE command_key=? AND state='dispatching' AND attempts=?",
            (
                json.dumps(response, sort_keys=True),
                response.get("flow_run_id"),
                now,
                command_key,
                attempt,
            ),
        ).rowcount
        if not completed:
            raise RuntimeError("durable command lease was superseded before completion")
        if row and row[0] is not None:
            conn.execute(
                "UPDATE admission_queue SET state='done', finished_at=CURRENT_TIMESTAMP, reason=NULL "
                "WHERE id=?",
                (row[0],),
            )
        if approval_id is not None:
            conn.execute(
                "UPDATE pending_approvals SET status='approved', prefect_run_id=?, "
                "resolved_by=?, resolved_at=? WHERE id=? AND tenant=?",
                (response.get("flow_run_id"), actor, now, approval_id, tenant),
            )
        if freshness is not None:
            model_id, commit_sha = freshness
            conn.execute(
                "INSERT INTO model_freshness "
                "(model_id, latest_modelzoo_commit, last_retrain_commit, is_stale, "
                "retrain_triggered_at) VALUES (?, ?, ?, 0, ?) "
                "ON CONFLICT(model_id) DO UPDATE SET "
                "last_retrain_commit=excluded.latest_modelzoo_commit, is_stale=0, "
                "retrain_triggered_at=excluded.retrain_triggered_at",
                (model_id, commit_sha, commit_sha, now),
            )
        enqueue_event(
            event_topic,
            {"command_key": command_key, **event_payload, "actor": actor, "tenant": tenant},
            conn=conn,
            actor=actor,
            tenant=tenant,
        )
        if audit is not None:
            action, target, details = audit
            _audit(conn, actor, action, target, {"command_key": command_key, **details}, tenant)
        conn.commit()
    finally:
        conn.close()


def _fail_command(
    command_key: str, exc: Exception, *, attempt: int, approval_id: str | None = None
) -> None:
    """Persist a retryable command failure and release an approval claim."""
    now = datetime.utcnow().isoformat()
    conn = _get_db()
    try:
        conn.execute(begin_immediate("admission"))
        row = conn.execute(
            "SELECT admission_id, tenant FROM control_plane_commands WHERE command_key=?",
            (command_key,),
        ).fetchone()
        failed = conn.execute(
            "UPDATE control_plane_commands SET state='failed', last_error=?, updated_at=? "
            "WHERE command_key=? AND state='dispatching' AND attempts=?",
            (str(exc)[:1000], now, command_key, attempt),
        ).rowcount
        if not failed:
            conn.commit()
            return
        if row and row[0] is not None:
            conn.execute(
                "UPDATE admission_queue SET state='failed', finished_at=CURRENT_TIMESTAMP, reason=? "
                "WHERE id=?",
                (str(exc)[:1000], row[0]),
            )
        if approval_id is not None:
            conn.execute(
                "UPDATE pending_approvals SET status='pending' "
                "WHERE id=? AND tenant=? AND status='approving'",
                (approval_id, row[1]),
            )
        conn.commit()
    finally:
        conn.close()


# ─── Endpoints ───────────────────────────────────────────────────────────────


def _pending_approvals_count() -> int | None:
    """Pending approvals, or ``None`` when the store could not be read.

    Both readers below used to answer an unreadable store with ``0`` — the one value this platform
    encodes as "nothing is waiting". `exa status` prints its approval line only ``if
    pending_count:``, so the fabricated zero is rendered as *silence*, the identical output a
    genuinely empty queue produces; `exa production` reported the same zero inside a
    production-readiness check. A broken approval store was therefore indistinguishable from a
    clear one, at every surface, for as long as it stayed broken.

    ``None`` is "unknown", which each caller can say out loud. This is the same correction
    `metrics.py` already made for the Prometheus path, where a fallback age of 0 meant "none
    pending" and kept `ApprovalsStale` silent exactly when it mattered.
    """
    conn = None
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE status = 'pending'"
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read the approval store: %s", exc)
        return None
    finally:
        if conn:
            conn.close()


# ─── Dispatch target contract (plan P0.2 / finding B2) ───────────────────────
#
# Every retrain this service dispatches goes to one Prefect deployment. Whether that deployment
# exists, and whether it accepts what this service sends, is a fact about the platform that no
# test double can see — so it is probed (cheaply, outside the breaker, at most once a minute) and
# reported in /health instead of being discovered by the first operator whose retrain 404s.

# What `trigger_retrain`, the approval gate and ModelZoo auto-retrain always send. The target flow
# must accept every one of these, and must not *require* anything else.
_DISPATCH_SENDS = frozenset({"model_name", "dataset_cls_name", "is_dummy", "backend_name"})
_DISPATCH_TTL_SECONDS = 60.0
_DISPATCH_PROBE_TIMEOUT = 3.0
_dispatch_cache: tuple[float, dict[str, Any]] | None = None
_DISPATCH_LOCK = threading.Lock()


def _probe_dispatch_target() -> dict[str, Any]:
    import urllib.error  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    result: dict[str, Any] = {
        "deployment": PREFECT_DEPLOYMENT_NAME,
        "state": "unreachable",
        "detail": None,
        "parameters": None,
    }
    if "/" not in PREFECT_DEPLOYMENT_NAME:
        result.update(state="incompatible", detail="PREFECT_DEPLOYMENT_NAME must be 'flow/name'")
        return result
    flow_name, dep_name = PREFECT_DEPLOYMENT_NAME.split("/", 1)
    url = (
        f"{PREFECT_API_URL}/deployments/name/"
        f"{urllib.parse.quote(flow_name)}/{urllib.parse.quote(dep_name)}"
    )
    try:
        from examlops.service_auth import prefect_headers

        req = urllib.request.Request(
            url, headers={"Accept": "application/json", **prefect_headers()}
        )
        with urllib.request.urlopen(req, timeout=_DISPATCH_PROBE_TIMEOUT) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            result.update(
                state="missing", detail="not registered — run `exa pipeline deploy` to create it"
            )
        else:
            result["detail"] = f"Prefect answered HTTP {exc.code}"
        return result
    except Exception as exc:  # noqa: BLE001 - any transport failure is "unreachable", not a crash
        result["detail"] = f"Prefect unreachable: {type(exc).__name__}"
        return result

    schema = payload.get("parameter_openapi_schema") or {}
    accepted = set((schema.get("properties") or {}).keys())
    required = set(schema.get("required") or [])
    not_accepted = sorted(_DISPATCH_SENDS - accepted)
    unsatisfiable = sorted(required - _DISPATCH_SENDS)
    result["parameters"] = sorted(accepted)
    if not_accepted or unsatisfiable:
        problems = []
        if not_accepted:
            problems.append(f"flow does not accept {not_accepted}")
        if unsatisfiable:
            problems.append(f"flow requires {unsatisfiable}, which the control plane never sends")
        result.update(state="incompatible", detail="; ".join(problems))
    else:
        result["state"] = "ok"
    return result


def _dispatch_status(*, refresh: bool = False) -> dict[str, Any]:
    """The cached dispatch-target verdict: ok | missing | incompatible | unreachable."""
    global _dispatch_cache
    with _DISPATCH_LOCK:
        now = time.monotonic()
        if refresh or _dispatch_cache is None or now >= _dispatch_cache[0]:
            _dispatch_cache = (now + _DISPATCH_TTL_SECONDS, _probe_dispatch_target())
        return dict(_dispatch_cache[1])


def _check_dispatch_parameters(parameters: dict[str, Any]) -> None:
    """Reject keys the dispatch target cannot accept, before any durable state is written.

    Only when the target's schema is actually known: an unreachable Prefect is not evidence that a
    parameter is wrong, so the request proceeds and Prefect's own validation has the last word.
    """
    accepted = _dispatch_status().get("parameters")
    if accepted is None:
        return
    unknown = sorted(set(parameters) - set(accepted))
    if unknown:
        raise HTTPException(
            400,
            f"Parameters {unknown} are not accepted by {PREFECT_DEPLOYMENT_NAME!r}; "
            f"accepted: {accepted}",
        )


def _runtime_capabilities() -> dict[str, Any]:
    """Describe the controls this process actually uses, not only configured target backends."""
    coordinator_name = os.getenv("EXAMLOPS_COORDINATOR", "db").strip().lower()
    publisher_name = os.getenv("EXAMLOPS_EVENT_PUBLISHER", "log").strip().lower()
    # What must be shared for a second replica to be safe. Not blockers, and reported as notes: the
    # Prefect circuit breaker is per replica (each protects itself — the usual design), and a
    # synchronous POST /retrain without an Idempotency-Key is a new request on every retry, on one
    # replica as on three. Retrain dedup, rate limits, leases, settings and the outbox are shared.
    blockers: list[str] = []
    if CONTROL_PLANE_STATE_BACKEND != "postgres":
        blockers.append("state_not_shared")
    if coordinator_name == "db" and CONTROL_PLANE_STATE_BACKEND != "postgres":
        blockers.append("coordination_not_cross_host")
    if EVENT_RELAY_SECONDS <= 0:
        blockers.append("event_relay_disabled")
    if publisher_name == "log":
        blockers.append("event_publisher_process_local")
    elif publisher_name == "kafka":
        blockers.append("event_publisher_not_implemented")
    try:
        outbox: dict[str, Any] = _shared_outbox_stats()
        oldest_age = _shared_outbox_oldest_age()
        outbox["oldest_pending_age_seconds"] = None if oldest_age is None else round(oldest_age, 1)
    except Exception as exc:  # noqa: BLE001 - health must report an unreadable outbox, not fail
        logger.warning("Could not read event outbox statistics: %s", exc)
        outbox = {"pending": None, "published": None, "poison": None, "error": "unavailable"}
    return {
        "state_backend": CONTROL_PLANE_STATE_BACKEND,
        "configured_coordinator": coordinator_name,
        "configured_event_publisher": publisher_name,
        "active_coordination": coordinator_name,
        "poller_enabled": MODELZOO_POLL_SECONDS > 0,
        "poller_has_lease": _poller_has_lease,
        "event_relay_enabled": EVENT_RELAY_SECONDS > 0,
        "event_relay_last_result": _relay_last_result,
        "event_relay_error": _relay_last_error,
        "outbox": outbox,
        "serving_snapshot": (
            _snapshot_projector.status() if _snapshot_projector is not None else {"enabled": False}
        ),
        "horizontal_scaling_safe": not blockers,
        "horizontal_scaling_blockers": blockers,
        "horizontal_scaling_notes": [
            "circuit_breaker_per_replica",
            "legacy_retrain_without_idempotency_key_is_a_new_request",
        ],
        # Honest about the one hole the rule cannot close: every holder of the shared legacy token
        # is the same principal, so requester and approver are indistinguishable for it.
        # `on` | `warn` | `off` (plan P3.2): whether the shared all-scopes token is still accepted.
        "legacy_token": LEGACY_TOKEN_MODE,
        "separation_of_duties": (
            "enforced-except-legacy-token" if SEPARATION_OF_DUTIES else "disabled"
        ),
        "command_workers": COMMAND_WORKERS,
        "command_worker_error": _command_worker_last_error,
    }


@app.get("/ready", include_in_schema=False)
def ready() -> dict[str, str]:
    """Compatibility alias for the original process-liveness endpoint."""
    return {"status": "alive"}


@app.get("/livez", include_in_schema=False)
def livez() -> dict[str, str]:
    """Process liveness: HTTP 200 means the server can answer and may be restarted if it cannot."""
    return {"status": "alive"}


@app.get("/health")
def health() -> dict[str, Any]:
    pending_count = _pending_approvals_count()

    poller_enabled = MODELZOO_POLL_SECONDS > 0
    poller_info: dict[str, Any] = {"enabled": poller_enabled}
    if poller_enabled:
        poller_info["last_ok_seconds_ago"] = (
            int(time.time() - _poller_last_ok_ts) if _poller_last_ok_ts else None
        )
        poller_info["stale"] = _is_poller_stale()
        poller_info["leader"] = _poller_has_lease
        poller_info["coordination_error"] = _poller_coordination_error

    # `all()` over an empty dict is vacuously true, so an unpopulated `_startup_checks`
    # used to publish `status: "ok"` — a machine-readable all-clear from a process that had
    # not run a single check (seen with `uvicorn --lifespan off`, and in any harness that
    # mounts the app without entering the lifespan). Not-yet-checked is `starting`, which is
    # honest and still 200, so a container healthcheck that only reads the code is unaffected.
    _recheck_failed_startup()
    if not _startup_checks:
        status = "starting"
    else:
        status = "ok" if all(v == "ok" for v in _startup_checks.values()) else "degraded"
    # A store this process cannot read is a degraded control plane, whatever the startup checks
    # concluded once at boot. Without this, `status` stayed "ok" beside a null count, and
    # `exa production` — which gates on `status == "ok"` — passed a platform whose approval queue
    # nobody could see.
    if pending_count is None:
        status = "degraded"
    if poller_enabled and _poller_coordination_error:
        status = "degraded"
    # Events are not being delivered. Reported, alerted on, and deliberately not a readiness
    # failure: they wait in the durable outbox and go out when the broker is back.
    if EVENT_RELAY_SECONDS > 0 and _relay_last_error:
        status = "degraded"
    # Readiness is decided before the dispatch verdict is folded in. A retrain target that is not
    # deployed yet makes every retrain fail, so `status` must not say "ok" — but it is not a reason
    # to pull the API out of rotation: approvals, reads and the durable command record still work,
    # and on a fresh stack the control plane legitimately starts before `exa pipeline deploy` runs.
    # Readiness is narrower than health on purpose: it answers "should this replica receive
    # requests", not "is everything well". A check outside `_READINESS_CHECKS` makes /health
    # degraded and leaves the replica in rotation, because the API still serves without it:
    #   - the event publisher: events are written to the durable outbox first and published after,
    #     so a broker outage (or a missing optional dependency) delays delivery and refuses nothing.
    #     Pulling every replica for it turns a degraded bus into an unavailable API — the backbone
    #     chaos drill found exactly that;
    #   - the registry warning ("no enabled models"), which a fresh stack has before its first
    #     model is added;
    #   - identity federation, where a bad trust file refuses federated tokens while static
    #     credentials keep working.
    # The store and the credential are different: without them this replica can serve nothing.
    unready = [name for name in _READINESS_CHECKS if _startup_checks.get(name, "ok") != "ok"]
    ready = (
        bool(_startup_checks)
        and not unready
        and pending_count is not None
        and not (poller_enabled and _poller_coordination_error)
        and not (poller_enabled and _is_poller_stale())
    )
    dispatch = _dispatch_status()
    if dispatch["state"] in ("missing", "incompatible"):
        status = "degraded"
    return {
        "status": status,
        "ready": ready,
        "dispatch": dispatch,
        "prefect_api_url": PREFECT_API_URL,
        "deployment": PREFECT_DEPLOYMENT_NAME,
        "auth_configured": _auth_is_usable(),
        "models": _get_registry(),
        "pending_approvals": pending_count,
        "startup_checks": _startup_checks,
        "poller": poller_info,
        "circuit_breaker": {"state": _prefect_breaker.state},
        "runtime": _runtime_capabilities(),
    }


@app.get("/readyz", include_in_schema=False)
def readyz() -> JSONResponse:
    """Traffic readiness: return non-2xx unless runtime dependencies are safe to use.

    ``/health`` remains a diagnostic endpoint with a stable 200 response and a verdict in its JSON
    body. Orchestrators need the verdict encoded in the HTTP status, which is what this endpoint
    provides. A stale enabled poller is also not ready: routing more traffic to a replica whose
    reconciliation loop has stopped only makes recovery less likely.
    """
    try:
        payload = health()
    except Exception as exc:  # noqa: BLE001 - readiness must fail closed on diagnostic bugs
        logger.error("Readiness evaluation failed: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "reason": "health evaluation failed"},
        )

    # `ready` is the traffic verdict computed by health() before the dispatch target is considered;
    # see the comment there. It already folds in a stale poller.
    ready_now = payload.get("ready") is True
    if ready_now:
        return JSONResponse(status_code=status.HTTP_200_OK, content=payload)
    payload["status"] = "not_ready"
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload)


@app.get("/status", dependencies=[Depends(_require_read_context)])
def platform_status() -> dict[str, Any]:
    """Improvement 4: all pings concurrent via ThreadPoolExecutor."""
    import urllib.request  # noqa: PLC0415

    def _ping(url: str, timeout: float = 5.0) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
                return r.status < 400
        except Exception:
            return False

    def _ping_json(url: str) -> tuple[bool, Any]:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5.0) as r:  # noqa: S310
                return r.status < 400, json.loads(r.read().decode())
        except Exception:
            return False, None

    pending_count = _pending_approvals_count()

    with ThreadPoolExecutor(max_workers=5) as pool:
        f_mlflow = pool.submit(_ping, f"{MLFLOW_URL}/health")
        f_prefect = pool.submit(_ping, f"{PREFECT_API_URL}/health")
        f_ray = pool.submit(_ping_json, f"{RAY_SERVE_URL}/models")
        f_dashboard = pool.submit(_ping, f"{DASHBOARD_URL}/api/health", 12.0)

        # Hard deadline on each result so a ping that hangs below the socket layer
        # (connect never returns) can't pin a worker thread past the ping budget.
        def _resolve(fut, default, deadline=15.0):
            try:
                return fut.result(timeout=deadline)
            except FuturesTimeoutError:
                return default

        mlflow_ok = _resolve(f_mlflow, False)
        prefect_ok = _resolve(f_prefect, False)
        ray_ok, ray_data = _resolve(f_ray, (False, None))
        dashboard_ok = _resolve(f_dashboard, False)

    ray_models: list[str] = (
        [m.get("name", m) if isinstance(m, dict) else m for m in (ray_data or [])] if ray_ok else []
    )
    # Report *which address was probed*, not just the verdict. The control plane pings its own
    # in-network peers (`http://mlflow:5000` under compose), while `exa status` printed the host
    # port map (`:15000`) beside each result — so a reader was told an address that had not been
    # checked and, on a remote deployment, could not be opened either. Additive: an older client
    # ignores the field, a newer client falls back when it is absent.
    return {
        "services": {
            "control_plane": {"ok": True, "url": "self"},
            "mlflow": {"ok": mlflow_ok, "url": f"{MLFLOW_URL}/health"},
            "prefect": {"ok": prefect_ok, "url": f"{PREFECT_API_URL}/health"},
            "ray_serve": {"ok": ray_ok, "models": ray_models, "url": f"{RAY_SERVE_URL}/models"},
            "dashboard": {"ok": dashboard_ok, "url": f"{DASHBOARD_URL}/api/health"},
        },
        "pending_approvals": pending_count,
    }


@app.get(
    "/models",
    response_model=list[ModelEntry],
    dependencies=[Depends(_require_read_context)],
)
def list_models() -> list[ModelEntry]:
    return [ModelEntry(model_name=n, datasets=d) for n, d in _get_registry().items()]


@app.post(
    "/retrain",
    response_model=RetrainResponse,
    dependencies=[Depends(_check_rate_limit)],
)
def trigger_retrain(
    req: RetrainRequest,
    x_idempotency_key: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
    context: RequestContext = Depends(_require_action("retrain")),
    http_request: Request = None,  # type: ignore[assignment]  # injected by FastAPI
) -> RetrainResponse:
    """Improvements 5 (dedup), 13 (metrics), 17 (idempotency), 12 (circuit breaker)."""
    _project_gate.enforce_model(context, req.model_name)  # ADR 0014 d4: before any lookup
    _policy_gate.enforce_retrain(req, context, http_request)  # ADR 0079: after the scope check
    registry = _get_registry()
    if req.model_name not in registry:
        raise HTTPException(400, f"Unknown model {req.model_name!r}. Known: {sorted(registry)}")
    if req.dataset_name not in registry[req.model_name]:
        raise HTTPException(
            400,
            f"Dataset {req.dataset_name!r} not supported by {req.model_name}. "
            f"Supported: {registry[req.model_name]}",
        )

    parameters: dict[str, Any] = {
        "model_name": req.model_name,
        "dataset_cls_name": req.dataset_name,
        "is_dummy": req.is_dummy,
        "backend_name": req.backend_name,
        **req.parameters,
    }

    # Fail on a key the dispatch target cannot accept before any lock or durable row exists.
    _check_dispatch_parameters(parameters)

    # Improvement 5: process-local fast-path deduplication. The durable command claim below is the
    # cross-process authority; this set only avoids needless database traffic within one worker.
    key = f"{context.tenant}:{_retrain_key(req.model_name, req.dataset_name)}"
    with _RETRAIN_LOCK:
        if key in _inflight_retrains:
            _metrics.record_retrain(req.model_name, req.dataset_name, "dedup")
            raise HTTPException(409, f"Retrain already in-flight for {key}")
        _inflight_retrains.add(key)

    lock_key = f"control-plane:retrain:{key}"
    holder = f"{_instance_id}:retrain:{uuid.uuid4().hex}"
    try:
        coordinator = _get_coordinator()
        acquired = coordinator.try_lock(lock_key, holder, RETRAIN_LOCK_SECONDS)
    except Exception as exc:
        with _RETRAIN_LOCK:
            _inflight_retrains.discard(key)
        logger.error("Shared retrain coordination unavailable: %s", exc)
        raise HTTPException(503, "Shared retrain coordination unavailable") from exc
    if not acquired:
        with _RETRAIN_LOCK:
            _inflight_retrains.discard(key)
        _metrics.record_retrain(req.model_name, req.dataset_name, "dedup")
        raise HTTPException(409, f"Retrain already in-flight for {key}")

    # Every outbound POST gets a stable key. A caller-supplied key survives process restarts and
    # replica changes; an omitted key preserves the historical "new request" semantics while still
    # making the gateway's own retries safe.
    # The IETF `Idempotency-Key` header, or the legacy `X-Idempotency-Key` (plan P1.7).
    x_idempotency_key = x_idempotency_key or idempotency_key
    external_key = (
        "retrain:"
        + hashlib.sha256(
            f"{context.tenant}\0{context.principal}\0{x_idempotency_key}".encode()
        ).hexdigest()
        if x_idempotency_key
        else f"retrain:{uuid.uuid4().hex}"
    )
    try:
        claim = _claim_command(
            external_key,
            "retrain",
            parameters,
            actor=context.principal,
            tenant=context.tenant,
        )
    except Exception:
        _release_retrain_guards(key, coordinator, lock_key, holder)
        raise
    if claim.outcome == "succeeded" and claim.response is not None:
        _release_retrain_guards(key, coordinator, lock_key, holder)
        _metrics.record_retrain(req.model_name, req.dataset_name, "dedup")
        return RetrainResponse(**claim.response)
    if claim.outcome == "busy":
        _release_retrain_guards(key, coordinator, lock_key, holder)
        _metrics.record_retrain(req.model_name, req.dataset_name, "dedup")
        raise HTTPException(409, "An identical retrain command is already being dispatched")
    if claim.outcome == "capacity":
        _release_retrain_guards(key, coordinator, lock_key, holder)
        _metrics.record_retrain(req.model_name, req.dataset_name, "throttled")
        raise _admission_refused()

    # Improvement 13: retrain metrics
    start_ts = time.monotonic()
    try:
        gateway = _get_gateway()
        flow_run_id = _dispatch_flow_run(gateway, parameters, external_key)
        response_data: dict[str, Any] = {
            "flow_run_id": flow_run_id,
            "deployment": PREFECT_DEPLOYMENT_NAME,
            "status_url": f"/retrain/{flow_run_id}",
            "parameters": parameters,
        }
        _complete_command(
            external_key,
            response_data,
            event_topic="retrain.scheduled",
            event_payload={
                "model_name": req.model_name,
                "dataset_name": req.dataset_name,
                "flow_run_id": flow_run_id,
            },
            attempt=claim.attempt or 0,
            audit=(
                "retrain_dispatched",
                req.model_name,
                {
                    "dataset": req.dataset_name,
                    "flow_run_id": flow_run_id,
                    **_credential_details(context),
                },
            ),
        )
    except Exception as exc:
        _fail_command(external_key, exc, attempt=claim.attempt or 0)
        _metrics.record_retrain(req.model_name, req.dataset_name, "error")
        raise
    finally:
        _release_retrain_guards(key, coordinator, lock_key, holder)
        duration = time.monotonic() - start_ts
        _metrics.observe_retrain_duration(req.model_name, req.dataset_name, duration)

    _metrics.record_retrain(req.model_name, req.dataset_name, "success")
    logger.info(
        "Scheduled retrain model=%s dataset=%s flow_run_id=%s",
        req.model_name,
        req.dataset_name,
        flow_run_id,
    )

    return RetrainResponse(**response_data)


@app.get(
    "/retrain/{flow_run_id}",
    response_model=FlowRunStatus,
)
def retrain_status(
    flow_run_id: str,
    context: RequestContext = Depends(_require_read_context),
) -> FlowRunStatus:
    # Structured credentials may inspect only runs dispatched for their verified tenant. The
    # legacy credential keeps its historical operator-wide lookup behavior during migration.
    if not context.is_legacy:
        conn = _get_db()
        try:
            owned = conn.execute(
                "SELECT 1 FROM control_plane_commands WHERE prefect_run_id=? AND tenant=?",
                (flow_run_id, context.tenant),
            ).fetchone()
        finally:
            conn.close()
        if owned is None:
            raise HTTPException(404, "Flow run not found")
    payload = _get_gateway().get_flow_run(flow_run_id)
    state = payload.get("state") or {}
    state_type = state.get("type")
    return FlowRunStatus(
        flow_run_id=flow_run_id,
        state_type=state_type,
        state_name=state.get("name"),
        is_terminal=state_type in ("COMPLETED", "FAILED", "CANCELLED", "CRASHED"),
    )


@app.post("/webhooks/modelzoo/gitlab")
async def webhook_gitlab(
    request: Request,
    x_gitlab_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _verify_gitlab_token(x_gitlab_token)
    try:
        payload = json.loads(await _read_webhook_body(request))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Webhook body must be a JSON object")
    ref = payload.get("ref", "")
    if not isinstance(ref, str) or not ref.endswith(f"/{MODELZOO_WATCH_BRANCH}"):
        return {"skipped": True, "reason": f"branch {ref!r} is not {MODELZOO_WATCH_BRANCH!r}"}
    commits = payload.get("commits", [])
    commit_sha = commits[0]["id"] if commits else payload.get("after", "")
    if not commit_sha:
        return {"skipped": True, "reason": "no commit SHA in payload"}
    pushed_by = payload.get("user_name") or (
        commits[0].get("author", {}).get("name") if commits else "unknown"
    )
    # _record_push_event does blocking SQLite writes and may fire a CI trigger /
    # auto-retrain (blocking HTTP with retries) — run it off the event loop so a
    # slow GitLab/Prefect call can't freeze the whole control plane.
    return await asyncio.to_thread(
        _record_push_event,
        commit_sha,
        MODELZOO_WATCH_BRANCH,
        pushed_by or "unknown",
        json.dumps(payload),
    )


@app.post("/webhooks/modelzoo/github")
async def webhook_github(request: Request) -> dict[str, Any]:
    raw_body = await _read_webhook_body(request)
    _verify_github_signature(raw_body, request.headers.get("x-hub-signature-256"))
    try:
        payload = json.loads(raw_body)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Webhook body must be a JSON object")
    ref = payload.get("ref", "")
    if not isinstance(ref, str) or not ref.endswith(f"/{MODELZOO_WATCH_BRANCH}"):
        return {"skipped": True, "reason": f"branch {ref!r} is not {MODELZOO_WATCH_BRANCH!r}"}
    head = payload.get("head_commit") or {}
    commit_sha = head.get("id", payload.get("after", ""))
    if not commit_sha:
        return {"skipped": True, "reason": "no commit SHA in payload"}
    return await asyncio.to_thread(
        _record_push_event,
        commit_sha,
        MODELZOO_WATCH_BRANCH,
        (payload.get("pusher") or {}).get("name", "unknown"),
        raw_body.decode(),
    )


@app.get("/models/{name}/meta", dependencies=[Depends(_require_read_context)])
def get_model_meta_endpoint(name: str) -> dict[str, Any]:
    if _model_meta_mod is None:
        raise HTTPException(503, "Model metadata service not available")
    try:
        m = _model_meta_mod.get_model_meta(name)
    except LookupError as exc:
        raise HTTPException(404, f"Unknown model {name!r}") from exc
    return {
        "name": m.name,
        "task_type": m.task_type,
        "estimator_class": m.estimator_class,
        "supported_datasets": m.supported_datasets,
        "input_schema": m.input_schema,
        "output_schema": m.output_schema,
        "promotion": m.promotion,
        "path_in_repo": m.path_in_repo,
        "bundled_images": m.bundled_images,
        # Extended fields for dashboard display
        "dataplane_bus_uuid": m.dataplane_bus_uuid,
        "hyperparameters": m.hyperparameters,
        "prefect": m.prefect,
        "enabled": m.enabled,
    }


@app.get("/models/{name}/readme", dependencies=[Depends(_require_read_context)])
def get_model_readme(name: str) -> dict[str, str]:
    if _model_meta_mod is None:
        raise HTTPException(503, "Model metadata service not available")
    text, sha = _model_meta_mod.read_readme(name)
    return {"text": text, "sha": sha}


@app.get("/models/{name}/images/{filename}", dependencies=[Depends(_require_read_context)])
def get_model_bundled_image(name: str, filename: str) -> Response:
    if _model_meta_mod is None:
        raise HTTPException(503, "Model metadata service not available")
    found = _model_meta_mod.read_image(name, filename)
    if found is None:
        raise HTTPException(404, "Image not found")
    data, content_type = found
    return Response(content=data, media_type=content_type)


@app.post(
    "/api/changes",
    dependencies=[Depends(_check_rate_limit)],
)
def notify_changes(
    notification: ChangeNotification,
    context: RequestContext = Depends(_require_action("changes")),
) -> dict[str, Any]:
    # ADR 0014 d4: a CI credential opens approvals only for models of projects it may edit.
    _project_gate.enforce_models(context, notification.model_ids)
    created: list[str] = []
    created_model_ids: list[str] = []
    now = datetime.utcnow().isoformat()
    changed_files_json = json.dumps(notification.changed_files)

    conn = _get_db()
    try:
        conn.execute(begin_immediate("approvals"))
        for model_id in notification.model_ids:
            existing = conn.execute(
                "SELECT id FROM pending_approvals "
                "WHERE tenant = ? AND model_id = ? AND status = 'pending'",
                (context.tenant, model_id),
            ).fetchone()
            if existing:
                logger.info(
                    "Skipping duplicate pending approval model=%s (id=%s)",
                    model_id,
                    existing[0],
                )
                continue
            row_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO pending_approvals "
                "(id, model_id, commit_sha, commit_msg, changed_files, status, tenant, "
                "requested_by, requested_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    row_id,
                    model_id,
                    notification.commit_sha,
                    notification.commit_msg,
                    changed_files_json,
                    context.tenant,
                    context.principal,
                    now,
                ),
            )
            _audit(
                conn,
                context.principal,
                "approval_requested",
                model_id,
                {
                    "approval_id": row_id,
                    "commit_sha": notification.commit_sha,
                    **_credential_details(context),
                },
                context.tenant,
            )
            created.append(row_id)
            created_model_ids.append(model_id)
            logger.info(
                "Created pending approval id=%s model=%s commit=%s",
                row_id,
                model_id,
                notification.commit_sha,
            )
        conn.commit()
        pending_count: int = conn.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE tenant=? AND status='pending'",
            (context.tenant,),
        ).fetchone()[0]
    finally:
        conn.close()

    for mid in created_model_ids:
        _metrics.record_created(mid, pending_count)
    return {"created": created}


@app.get("/approvals", response_model=list[ApprovalEntry])
def list_approvals(
    status: str | None = None,
    context: RequestContext = Depends(_require_read_context),
) -> list[ApprovalEntry]:
    conn = _get_db()
    try:
        sql = (
            "SELECT id, model_id, commit_sha, commit_msg, changed_files, "
            "status, prefect_run_id, reject_reason, tenant, requested_by, resolved_by, "
            "requested_at, resolved_at "
            "FROM pending_approvals"
        )
        rows = (
            conn.execute(
                sql + " WHERE tenant = ? AND status = ? ORDER BY requested_at DESC",
                (context.tenant, status),
            ).fetchall()
            if status
            else conn.execute(
                sql + " WHERE tenant = ? ORDER BY requested_at DESC", (context.tenant,)
            ).fetchall()
        )
    finally:
        conn.close()

    entries: list[ApprovalEntry] = []
    for row in rows:
        (
            row_id,
            model_id,
            commit_sha,
            commit_msg,
            changed_files_raw,
            row_status,
            prefect_run_id,
            reject_reason,
            tenant,
            requested_by,
            resolved_by,
            requested_at,
            resolved_at,
        ) = row
        try:
            changed_files = json.loads(changed_files_raw) if changed_files_raw else []
        except (TypeError, ValueError):
            changed_files = []
        entries.append(
            ApprovalEntry(
                id=row_id,
                model_id=model_id,
                commit_sha=commit_sha,
                commit_msg=commit_msg,
                changed_files=changed_files,
                status=row_status,
                prefect_run_id=prefect_run_id,
                reject_reason=reject_reason,
                tenant=tenant,
                requested_by=requested_by,
                resolved_by=resolved_by,
                requested_at=requested_at,
                resolved_at=resolved_at,
            )
        )
    return entries


@app.post(
    "/approve/{model_id}",
    dependencies=[Depends(_check_rate_limit)],
)
def approve_model(
    model_id: str,
    context: RequestContext = Depends(_require_action("approve")),
    http_request: Request = None,  # type: ignore[assignment]  # injected by FastAPI
) -> dict[str, Any]:
    _project_gate.enforce_model(context, model_id)  # ADR 0014 d4: before any lookup
    # ADR 0079: after the scope check — same action/keys as `exa approvals approve` + dashboard.
    _policy_gate.enforce_approval("approval_approve", model_id, None, context, http_request)
    # Improvement 16: reject expired entries before approving
    _expire_old_approvals()

    # Find either a new approval or a previously interrupted dispatch. The durable command lease
    # below decides whether an ``approving`` row is still owned or is safe to recover.
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT id, status, requested_by FROM pending_approvals "
            "WHERE tenant = ? AND model_id = ? AND status IN ('pending', 'approving') "
            "ORDER BY requested_at DESC LIMIT 1",
            (context.tenant, model_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"No pending approval found for model {model_id!r}")
        row_id = row[0]
        approval_status = row[1]
        requested_by = row[2]
    finally:
        conn.close()

    if SEPARATION_OF_DUTIES and not context.is_legacy and requested_by == context.principal:
        raise HTTPException(
            403,
            f"Separation of duties: {context.principal!r} requested this change and cannot "
            "approve it; another principal must.",
        )

    registry = _get_registry()
    datasets = registry.get(model_id, [])
    if not datasets:
        raise HTTPException(400, f"Model {model_id!r} has no registered datasets")
    dataset_name = datasets[0]

    parameters: dict[str, Any] = {
        "model_name": model_id,
        "dataset_cls_name": dataset_name,
        "is_dummy": False,
        "backend_name": None,
    }

    command_key = f"approval:{row_id}"
    if approval_status == "approving":
        conn = _get_db()
        try:
            command_exists = conn.execute(
                "SELECT 1 FROM control_plane_commands WHERE command_key=?", (command_key,)
            ).fetchone()
        finally:
            conn.close()
        # An ``approving`` row created by an older release has no stable Prefect idempotency key.
        # Retrying it automatically could duplicate a flow, so leave it for explicit reconciliation.
        if command_exists is None:
            raise HTTPException(409, f"Approval for model {model_id!r} is already in progress")
    claim = _claim_command(
        command_key,
        "approval",
        parameters,
        approval_id=row_id,
        actor=context.principal,
        tenant=context.tenant,
    )
    if claim.outcome == "succeeded" and claim.response is not None:
        return claim.response
    if claim.outcome == "busy":
        raise HTTPException(409, f"Approval for model {model_id!r} is already in progress")
    if claim.outcome == "capacity":
        raise _admission_refused()

    gateway = _get_gateway()
    try:
        flow_run_id: str = _dispatch_flow_run(gateway, parameters, command_key)
        response = {
            "flow_run_id": flow_run_id,
            "status_url": f"/retrain/{flow_run_id}",
            "model_id": model_id,
        }
        _complete_command(
            command_key,
            response,
            event_topic="approval.approved",
            event_payload={
                "approval_id": row_id,
                "model_id": model_id,
                "flow_run_id": flow_run_id,
            },
            attempt=claim.attempt or 0,
            approval_id=row_id,
            audit=(
                "approval_approved",
                model_id,
                {
                    "approval_id": row_id,
                    "flow_run_id": flow_run_id,
                    "requested_by": requested_by,
                    **_credential_details(context),
                },
            ),
        )
    except Exception as exc:
        _fail_command(command_key, exc, attempt=claim.attempt or 0, approval_id=row_id)
        raise

    pending_count = _pending_approvals_count() or 0

    _metrics.record_approved(model_id, pending_count)
    logger.info("Approved model=%s approval_id=%s flow_run_id=%s", model_id, row_id, flow_run_id)
    return response


@app.post(
    "/reject/{model_id}",
    dependencies=[Depends(_check_rate_limit)],
)
def reject_model(
    model_id: str,
    body: RejectRequest = RejectRequest(),
    context: RequestContext = Depends(_require_action("approve")),
    http_request: Request = None,  # type: ignore[assignment]  # injected by FastAPI
) -> dict[str, Any]:
    _project_gate.enforce_model(context, model_id)  # ADR 0014 d4: before any lookup
    _policy_gate.enforce_approval(  # ADR 0079: same action/keys as `exa approvals reject`
        "approval_reject", model_id, getattr(body, "reason", None), context, http_request
    )
    pending_count = 0
    conn = _get_db()
    try:
        conn.execute(begin_immediate("approvals"))
        row = conn.execute(
            "SELECT id FROM pending_approvals "
            "WHERE tenant = ? AND model_id = ? AND status = 'pending' "
            "ORDER BY requested_at DESC LIMIT 1",
            (context.tenant, model_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"No pending approval found for model {model_id!r}")
        row_id = row[0]
        # Conditional on `pending`: approval claims run under the `admission` lock scope, not
        # this one, so the row may have moved to `approving` since the SELECT above. An
        # unconditional UPDATE would mark a dispatching approval rejected (plan P1.3).
        rejected = conn.execute(
            "UPDATE pending_approvals SET status='rejected', reject_reason=?, resolved_by=?, "
            "resolved_at=? WHERE id=? AND status='pending'",
            (body.reason, context.principal, datetime.utcnow().isoformat(), row_id),
        ).rowcount
        if not rejected:
            raise HTTPException(409, f"Approval for model {model_id!r} is no longer pending")
        # The rejection is a decision like an approval: same outbox stream, same audit chain,
        # same transaction (plan P0.8). It used to emit neither.
        enqueue_event(
            "approval.rejected",
            {
                "approval_id": row_id,
                "model_id": model_id,
                "reason": body.reason,
                "actor": context.principal,
                "tenant": context.tenant,
            },
            conn=conn,
            actor=context.principal,
            tenant=context.tenant,
        )
        _audit(
            conn,
            context.principal,
            "approval_rejected",
            model_id,
            {"approval_id": row_id, "reason": body.reason, **_credential_details(context)},
            context.tenant,
        )
        conn.commit()
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE tenant=? AND status='pending'",
            (context.tenant,),
        ).fetchone()[0]
    finally:
        conn.close()

    _metrics.record_rejected(model_id, pending_count)
    logger.info("Rejected model=%s approval_id=%s reason=%s", model_id, row_id, body.reason)
    return {"model_id": model_id, "status": "rejected"}


def _enforce_row_model(context: RequestContext, kind: str, row_id: str) -> None:
    """ADR 0014 d4 for routes addressed by an approval or command id: authorize its model.

    Only read with multi-tenancy on (flag off = no extra query). An id that does not exist in the
    caller's tenant names no model, so the handler answers 404 as it always did.
    """
    from examlops import authz  # noqa: PLC0415

    if not authz.multitenancy_enabled():
        return
    conn = _get_db()
    try:
        if kind == "approval":
            row = conn.execute(
                "SELECT model_id FROM pending_approvals WHERE id=? AND tenant=?",
                (row_id, context.tenant),
            ).fetchone()
            model = row[0] if row else None
        else:
            row = conn.execute(
                "SELECT payload FROM control_plane_commands WHERE command_key=? AND tenant=?",
                (row_id, context.tenant),
            ).fetchone()
            try:
                model = json.loads(row[0])["parameters"].get("model_name") if row else None
            except (TypeError, ValueError, KeyError, AttributeError):
                model = None
    finally:
        conn.close()
    if row is None:
        return
    if not model:  # a row whose model cannot be read is refused, never waved through
        raise HTTPException(403, f"Cannot determine the model this {kind} acts on; refusing")
    _project_gate.enforce_model(context, str(model))


@app.delete(
    "/approvals/{approval_id}",
    dependencies=[Depends(_check_rate_limit)],
)
def retract_approval(
    approval_id: str,
    context: RequestContext = Depends(_require_action("approve")),
) -> dict[str, Any]:
    """Retract a pending approval (a stale or duplicate entry) without erasing it.

    `exa approvals delete` called this route for a long time before it existed (plan P0.3 /
    finding B3). It is a *retraction*, not a delete: an approval is a governance record, so the
    row stays, marked ``retracted`` with who and when, and an ``approval.retracted`` event goes to
    the outbox in the same transaction. Only a ``pending`` approval in the caller's tenant can be
    retracted; one being dispatched (``approving``) or already resolved answers 409.
    """
    _enforce_row_model(context, "approval", approval_id)  # ADR 0014 d4
    now = datetime.utcnow().isoformat()
    conn = _get_db()
    try:
        conn.execute(begin_immediate("approvals"))
        row = conn.execute(
            "SELECT model_id, status FROM pending_approvals WHERE id=? AND tenant=?",
            (approval_id, context.tenant),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"No approval {approval_id!r}")
        model_id, current = row[0], row[1]
        if current != "pending":
            raise HTTPException(409, f"Approval {approval_id!r} is {current}, not pending")
        conn.execute(
            "UPDATE pending_approvals SET status='retracted', resolved_by=?, resolved_at=? "
            "WHERE id=? AND tenant=? AND status='pending'",
            (context.principal, now, approval_id, context.tenant),
        )
        enqueue_event(
            "approval.retracted",
            {
                "approval_id": approval_id,
                "model_id": model_id,
                "actor": context.principal,
                "tenant": context.tenant,
            },
            conn=conn,
            actor=context.principal,
            tenant=context.tenant,
        )
        _audit(
            conn,
            context.principal,
            "approval_retracted",
            model_id,
            {"approval_id": approval_id, **_credential_details(context)},
            context.tenant,
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("Retracted approval id=%s model=%s by=%s", approval_id, model_id, context.principal)
    return {"id": approval_id, "model_id": model_id, "status": "retracted"}


# ─── /v1: asynchronous commands (plan P1.2 + P1.6) ───────────────────────────
#
# The legacy routes dispatch to Prefect inside the HTTP request: the caller waits on Prefect and a
# Prefect outage becomes a client timeout. /v1 accepts a command durably, answers 202 with a URL to
# follow, and a worker pool dispatches it — retrying with backoff, admitting under the same
# capacity rules, and giving up (`dead`) after COMMAND_MAX_ATTEMPTS. Errors on /v1 are RFC 9457
# problem documents.

COMMAND_WORKERS = max(0, int(os.getenv("CONTROL_PLANE_COMMAND_WORKERS", "1")))
COMMAND_POLL_SECONDS = max(0.1, float(os.getenv("CONTROL_PLANE_COMMAND_POLL_SECONDS", "1")))
COMMAND_MAX_ATTEMPTS = max(1, int(os.getenv("CONTROL_PLANE_COMMAND_MAX_ATTEMPTS", "5")))
COMMAND_BACKOFF_SECONDS = max(0.0, float(os.getenv("CONTROL_PLANE_COMMAND_BACKOFF_SECONDS", "5")))
_COMMAND_KINDS = frozenset({"retrain"})
_metrics.initialize_command_outcomes(_COMMAND_KINDS)
_TERMINAL_STATES = frozenset({"succeeded", "dead", "cancelled"})


def _command_view(row: Any) -> CommandView:
    key, kind, state, attempts, response, last_error, created_at, updated_at, run_state = row
    return CommandView(
        command_id=key,
        kind=kind,
        state=state,
        attempts=int(attempts or 0),
        result=json.loads(response) if response else None,
        last_error=last_error,
        created_at=created_at,
        updated_at=updated_at,
        status_url=f"/v1/commands/{key}",
        run_state=run_state,
    )


_VIEW_COLUMNS = (
    "command_key, kind, state, attempts, response, last_error, created_at, updated_at, run_state"
)


def _submit_command(
    command_key: str,
    kind: str,
    parameters: dict[str, Any],
    *,
    actor: str,
    tenant: str,
    exclusive: tuple[str, str] | None = None,
    auth: dict[str, str] | None = None,
) -> CommandView:
    """Durably accept an asynchronous command; idempotent on ``command_key``.

    ``auth`` (how the submitter authenticated, ADR 0125) is stored beside the request, outside its
    hash: the worker that dispatches later audits it, and a retry of the same request with another
    credential of the same principal is still the same request.

    ``exclusive`` (model, dataset) refuses the command with 409 while another command of the tenant
    is dispatching or training that pair. The check runs inside the same locked transaction as the
    insert: checked first and inserted after, two racing submissions — two replicas, or two
    threads of one — both saw "none in progress" and both dispatched.
    """
    payload, request_hash = _command_payload(kind, parameters)
    if auth:
        payload = json.dumps(
            {**json.loads(payload), "auth": auth}, sort_keys=True, separators=(",", ":")
        )
    now = datetime.utcnow().isoformat()
    conn = _get_db()
    try:
        conn.execute(begin_immediate("admission"))
        if exclusive is not None:
            active = _active_retrain(tenant, exclusive[0], exclusive[1], conn=conn)
            if active is not None and active != command_key:
                conn.rollback()
                _metrics.record_retrain(exclusive[0], exclusive[1], "dedup")
                raise HTTPException(
                    409,
                    f"A retrain of {exclusive[0]} on {exclusive[1]} is already in progress "
                    f"(command {active}); follow it at /v1/commands/{active}",
                )
        conn.execute(
            "INSERT OR IGNORE INTO control_plane_commands "
            "(command_key, kind, request_hash, payload, state, actor, tenant, mode, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, 'async', ?, ?)",
            (command_key, kind, request_hash, payload, actor, tenant, now, now),
        )
        row = conn.execute(
            "SELECT request_hash, actor, tenant, " + _VIEW_COLUMNS + " "
            "FROM control_plane_commands WHERE command_key=?",
            (command_key,),
        ).fetchone()
        if row[0] != request_hash:
            raise HTTPException(409, "Idempotency-Key was already used for different input")
        if row[1] != actor or row[2] != tenant:
            raise HTTPException(403, "Command belongs to a different principal or tenant")
        conn.commit()
        return _command_view(row[3:])
    finally:
        conn.close()


def _backoff_elapsed(attempts: int, updated_at: str) -> bool:
    if attempts <= 0:
        return True
    wait = min(300.0, COMMAND_BACKOFF_SECONDS * 2 ** (attempts - 1))
    try:
        return datetime.utcnow() >= datetime.fromisoformat(updated_at) + timedelta(seconds=wait)
    except ValueError:
        return True


def _mark_dead(command_key: str, attempt: int) -> None:
    conn = _get_db()
    try:
        conn.execute(begin_immediate("admission"))
        buried = conn.execute(
            "UPDATE control_plane_commands SET state='dead', updated_at=? "
            "WHERE command_key=? AND state='failed' AND attempts=?",
            (datetime.utcnow().isoformat(), command_key, attempt),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    if buried:
        _note_orphan_run(command_key)


#: When this replica last asked Prefect about a buried command, so a quiet platform does not re-ask
#: every cycle. Deliberately *not* "asked once": the run this looks for appears **after** the
#: burial — a dispatch that was merely slow landing late is what makes it an orphan — so a single
#: early answer of "no run" is the one answer that must not be final. Asking again every
#: `ORPHAN_RECHECK_SECONDS` until the window closes is what catches it; the first version of this
#: asked once, and the partition drill kept reporting no orphan because every replica had already
#: asked before the held dispatch landed.
_ORPHAN_CHECKED: dict[str, float] = {}
ORPHAN_CHECK_WINDOW_SECONDS = max(
    0.0, float(os.getenv("CONTROL_PLANE_ORPHAN_CHECK_WINDOW_SECONDS", "900"))
)
ORPHAN_RECHECK_SECONDS = max(1.0, float(os.getenv("CONTROL_PLANE_ORPHAN_RECHECK_SECONDS", "60")))


#: The sentence added to a dead command whose run was found, and the marker the sweep matches on.
_ORPHAN_NOTE = "a Prefect flow run exists for this command"


def _sweep_orphan_runs(limit: int = 5) -> None:
    """Ask Prefect about recently buried commands — from whichever replica is healthy.

    The check at burial is not enough, and the partition drill is what showed it: the replica that
    gives up on a command is usually the one that *cannot reach Prefect*, so its own lookup times
    out too and the orphan stays invisible. Every replica reconciles, so running the check here
    means a healthy one asks on its behalf.
    """
    if ORPHAN_CHECK_WINDOW_SECONDS <= 0:
        return
    since = (datetime.utcnow() - timedelta(seconds=ORPHAN_CHECK_WINDOW_SECONDS)).isoformat()
    conn = _get_db()
    try:
        # No `LIKE '%…%'` here, and the platform has none anywhere else in its shared SQL: psycopg
        # reads a literal `%` in a parameterised statement as a placeholder, so the query died on
        # Postgres with "only '%s', '%b', '%t' are allowed as placeholders, got '%f'" — every sweep
        # cycle, silently, because the worker swallows a bad cycle. Doubling it would break SQLite.
        # The note is matched in Python instead, which is portable and cheap at this row count.
        rows = conn.execute(
            "SELECT command_key, last_error FROM control_plane_commands "
            "WHERE state='dead' AND prefect_run_id IS NULL AND updated_at >= ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (since, limit * 8),
        ).fetchall()
    finally:
        conn.close()
    checked = 0
    now = time.monotonic()
    for row in rows:
        if _ORPHAN_NOTE in (row["last_error"] or ""):
            continue
        key = row["command_key"]
        asked_at = _ORPHAN_CHECKED.get(key)
        if asked_at is not None and now - asked_at < ORPHAN_RECHECK_SECONDS:
            continue
        _ORPHAN_CHECKED[key] = now
        _note_orphan_run(key)
        checked += 1
        if checked >= limit:
            break
    if len(_ORPHAN_CHECKED) > 2000:  # bounded: the window is minutes, the map must not be forever
        _ORPHAN_CHECKED.clear()


def _note_orphan_run(command_key: str) -> None:
    """Ask Prefect whether the command we just buried left a training run behind, and say so.

    Giving up on a command does not recall a dispatch that is merely slow: a replica cut off from
    Prefect spends its attempts while its last dispatch is still in flight, and that dispatch can
    land afterwards. The platform then holds the work as dead while a job for it runs, with nothing
    pointing at it — measured in
    `tests/integration/test_control_plane_partition_kind_live.py`, and until now only findable by
    hand.

    Best effort in every direction: the lookup is read-only, any failure leaves the command exactly
    as it was, and nothing here can make a burial fail. "No answer from Prefect" is recorded as not
    knowing, never as "no run".
    """
    try:
        run_id = _get_gateway().find_flow_run_by_key(command_key)
    except Exception as exc:  # noqa: BLE001 - a burial must not depend on Prefect answering
        logger.warning("Could not check %s for an orphaned flow run: %s", command_key, exc)
        return
    if not run_id:
        return
    _metrics.record_dead_command_with_run()
    logger.warning(
        "Command %s is dead but Prefect has flow run %s for it: a dispatch landed after the "
        "platform gave up. Check that run before resubmitting.",
        command_key,
        run_id,
    )
    note = f"{_ORPHAN_NOTE}: {run_id}"
    conn = _get_db()
    try:
        conn.execute(begin_immediate("admission"))
        conn.execute(
            "UPDATE control_plane_commands "
            "SET last_error = CASE WHEN last_error IS NULL OR last_error = '' THEN ? "
            "    ELSE last_error || '; ' || ? END "
            "WHERE command_key = ? AND state = 'dead'",
            (note, note, command_key),
        )
        conn.commit()
    finally:
        conn.close()


def _sweep_abandoned_claims(stale_before: str) -> None:
    """Claims whose dispatcher died: its lease ran out while the command was ``dispatching``.

    An asynchronous command is left for the worker to take over (it selects it below) unless it
    has already used every attempt: a command that kills each dispatcher is buried, not handed to
    the next replica. A synchronous command is never retried in the background: it is marked
    ``failed``, so it stops counting as a retrain in progress, and the caller's retry with the
    same key reclaims it and dispatches with the same Prefect idempotency key.
    """
    now = datetime.utcnow().isoformat()
    conn = _get_db()
    try:
        conn.execute(begin_immediate("admission"))
        _release_abandoned_admissions(conn, stale_before)
        buried = conn.execute(
            "UPDATE control_plane_commands SET state='dead', updated_at=?, "
            "last_error='abandoned: its dispatcher stopped on every attempt' "
            "WHERE mode='async' AND state='dispatching' AND updated_at <= ? AND attempts >= ?",
            (now, stale_before, COMMAND_MAX_ATTEMPTS),
        ).rowcount
        released = conn.execute(
            "UPDATE control_plane_commands SET state='failed', updated_at=?, "
            "last_error='abandoned: the replica dispatching it stopped; retry with the same "
            "Idempotency-Key' WHERE mode='sync' AND state='dispatching' AND updated_at <= ?",
            (now, stale_before),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    for _ in range(buried or 0):
        _metrics.record_command_outcome("retrain", "dead")
    if buried or released:
        logger.warning(
            "Abandoned command claims: %d buried after %d attempts, %d synchronous released",
            buried or 0,
            COMMAND_MAX_ATTEMPTS,
            released or 0,
        )


def _work_commands_once(limit: int = 20) -> int:
    """Dispatch up to ``limit`` due asynchronous commands, oldest first. Returns how many ran.

    Due means pending, failed and past its backoff, or claimed by a dispatcher whose lease ran
    out: a replica that crashed mid-dispatch leaves its claim, and another takes it over with the
    same Prefect idempotency key, so a run the crashed one did create is returned, not doubled.
    """
    stale_before = (datetime.utcnow() - timedelta(seconds=COMMAND_LEASE_SECONDS)).isoformat()
    _sweep_abandoned_claims(stale_before)
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT command_key, kind, payload, actor, tenant, attempts, state, updated_at "
            "FROM control_plane_commands WHERE mode='async' AND ("
            "state IN ('pending', 'failed') OR (state='dispatching' AND updated_at <= ?)) "
            "ORDER BY created_at ASC, command_key ASC LIMIT ?",
            (stale_before, limit),
        ).fetchall()
        depth = conn.execute(
            "SELECT COUNT(*) FROM control_plane_commands "
            "WHERE mode='async' AND state IN ('pending', 'failed')"
        ).fetchone()[0]
    finally:
        conn.close()
    _metrics.set_command_queue_depth(int(depth))
    ran = 0
    for key, kind, payload, actor, tenant, attempts, state, updated_at in rows:
        if kind not in _COMMAND_KINDS:
            continue
        if state == "failed" and not _backoff_elapsed(int(attempts), updated_at):
            continue
        document = json.loads(payload)
        parameters = document["parameters"]
        submitted_with = document.get("auth") or {}  # how the submitter authenticated
        claim = _claim_command(key, kind, parameters, actor=actor, tenant=tenant)
        if claim.outcome != "claimed":
            _metrics.record_command_outcome(kind, claim.outcome)
            if claim.outcome == "capacity":
                break  # the rest are younger; admission is full for now
            continue
        ran += 1
        attempt = claim.attempt or 0
        # The retrain metrics the synchronous route records, so HighRetrainErrorRate and
        # RetrainDurationP99High keep seeing retrains now that every platform caller submits here.
        model_label = str(parameters.get("model_name"))
        dataset_label = str(parameters.get("dataset_cls_name"))
        started = time.monotonic()
        # Bound before the try so the handler can tell the two failures apart: a dispatch that
        # never reached Prefect, and a dispatch that landed whose bookkeeping then failed. They
        # look identical from inside `except`, and merging them made a started retrain count as a
        # failed one.
        flow_run_id: str | None = None
        try:
            flow_run_id = _dispatch_flow_run(_get_gateway(), parameters, key)
            _complete_command(
                key,
                {"flow_run_id": flow_run_id, "deployment": PREFECT_DEPLOYMENT_NAME},
                event_topic="retrain.scheduled",
                event_payload={
                    "model_name": parameters.get("model_name"),
                    "dataset_name": parameters.get("dataset_cls_name"),
                    "flow_run_id": flow_run_id,
                },
                attempt=attempt,
                audit=(
                    "retrain_dispatched",
                    str(parameters.get("model_name")),
                    {
                        "dataset": parameters.get("dataset_cls_name"),
                        "flow_run_id": flow_run_id,
                        **submitted_with,
                    },
                ),
            )
            _metrics.record_command_outcome(kind, "succeeded")
            _metrics.record_retrain(model_label, dataset_label, "success")
            _metrics.observe_retrain_duration(
                model_label, dataset_label, time.monotonic() - started
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the command, retried or buried
            # `HighRetrainErrorRate` pages above 20% over 15 minutes and retrains are rare, so one
            # false error against one real retrain is 100%. A retrain that STARTED is therefore
            # never recorded as `error`: it is recorded as `dispatched_unrecorded`, which is what
            # actually happened — the training is running and the platform failed to write it down.
            # The command is still failed and retried; that part is correct and self-heals, because
            # the retry carries the same Prefect idempotency key and gets the same run back.
            dispatched = flow_run_id is not None
            _metrics.record_retrain(
                model_label, dataset_label, "dispatched_unrecorded" if dispatched else "error"
            )
            if dispatched:
                logger.warning(
                    "Command %s dispatched flow run %s but could not be recorded (%s); it will be "
                    "retried against the same Prefect idempotency key",
                    key,
                    flow_run_id,
                    exc,
                )
            _fail_command(key, exc, attempt=attempt)
            if attempt >= COMMAND_MAX_ATTEMPTS:
                _mark_dead(key, attempt)
                _metrics.record_command_outcome(kind, "dead")
                logger.error("Command %s dead after %d attempts: %s", key, attempt, exc)
            else:
                _metrics.record_command_outcome(kind, "failed")
                logger.warning("Command %s attempt %d failed: %s", key, attempt, exc)
    return ran


RECONCILE_SECONDS = max(1.0, float(os.getenv("CONTROL_PLANE_RECONCILE_SECONDS", "30")))
_RUN_TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED", "CRASHED", "MISSING"})
_RUN_ACTIVE_SQL = "('SCHEDULED', 'PENDING', 'RUNNING', 'PAUSED', 'CANCELLING')"


def _reconcile_runs_once(limit: int = 20) -> int:
    """Follow dispatched flow runs to a terminal state (plan P1.2b). Returns runs that settled.

    Each run is polled at most every RECONCILE_SECONDS. A run that reaches a terminal state is
    recorded on its command and published once (`retrain.run_<state>`), in the same transaction —
    the event a dashboard, the autopilot or a notification consumer can act on without polling
    Prefect themselves. A run Prefect no longer knows (404) settles as MISSING rather than being
    polled forever.
    """
    due = (datetime.utcnow() - timedelta(seconds=RECONCILE_SECONDS)).isoformat()
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT command_key, prefect_run_id, kind, payload FROM control_plane_commands "
            "WHERE prefect_run_id IS NOT NULL AND state='succeeded' "
            "AND (run_state IS NULL OR run_state NOT IN "
            "('COMPLETED', 'FAILED', 'CANCELLED', 'CRASHED', 'MISSING')) "
            "AND (run_state_at IS NULL OR run_state_at <= ?) "
            "ORDER BY run_state_at IS NOT NULL, run_state_at ASC LIMIT ?",
            (due, limit),
        ).fetchall()
    finally:
        conn.close()
    settled = 0
    gateway = _get_gateway()
    for key, run_id, kind, payload in rows:
        try:
            with _dispatch_budget():
                run = gateway.get_flow_run(run_id)
            run_state = ((run.get("state") or {}).get("type") or "UNKNOWN").upper()
        except HTTPException as exc:
            if exc.status_code == 404:
                run_state = "MISSING"
            else:
                logger.warning("Reconciler: cannot read flow run %s: %s", run_id, exc.detail)
                break  # Prefect is struggling; the breaker and the next cycle decide
        except Exception as exc:  # noqa: BLE001 - one unreadable run must not stop the cycle
            logger.warning("Reconciler: cannot read flow run %s: %s", run_id, exc)
            continue
        terminal = run_state in _RUN_TERMINAL
        now = datetime.utcnow().isoformat()
        conn = _get_db()
        try:
            conn.execute(begin_immediate("admission"))
            changed = conn.execute(
                "UPDATE control_plane_commands SET run_state=?, run_state_at=? "
                "WHERE command_key=? AND (run_state IS NULL OR run_state NOT IN "
                "('COMPLETED', 'FAILED', 'CANCELLED', 'CRASHED', 'MISSING'))",
                (run_state, now, key),
            ).rowcount
            if changed and terminal:
                parameters = json.loads(payload).get("parameters", {})
                row = conn.execute(
                    "SELECT actor, tenant FROM control_plane_commands WHERE command_key=?", (key,)
                ).fetchone()
                enqueue_event(
                    f"{kind}.run_{run_state.lower()}",
                    {
                        "command_key": key,
                        "flow_run_id": run_id,
                        "run_state": run_state,
                        "model_name": parameters.get("model_name"),
                        "dataset_name": parameters.get("dataset_cls_name"),
                        "actor": row[0],
                        "tenant": row[1],
                    },
                    conn=conn,
                    actor=row[0],
                    tenant=row[1],
                )
                settled += 1
            conn.commit()
        finally:
            conn.close()
    return settled


def _active_retrain(tenant: str, model: str, dataset: str, *, conn: Any = None) -> str | None:
    """A command of this tenant still dispatching or training this model × dataset, if any.

    The training lease (plan P1.2): dedup means "a run is in progress", not only "a dispatch is in
    flight". Resolved from the durable command table, so it holds across replicas and restarts.
    Pass the ``conn`` of a transaction that holds the admission lock to make check-and-insert atomic.
    """
    own = conn is None
    if own:
        conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT command_key, payload FROM control_plane_commands "
            "WHERE tenant=? AND kind='retrain' AND ("
            "state IN ('pending', 'dispatching') OR (state='failed' AND mode='async') OR "
            "(state='succeeded' AND (run_state IS NULL OR run_state IN " + _RUN_ACTIVE_SQL + ")))",
            (tenant,),
        ).fetchall()
    finally:
        if own:
            conn.close()
    for key, payload in rows:
        parameters = json.loads(payload).get("parameters", {})
        if parameters.get("model_name") == model and parameters.get("dataset_cls_name") == dataset:
            return str(key)
    return None


_command_worker_last_error: str | None = None


def _start_command_workers() -> None:
    if COMMAND_WORKERS <= 0:
        logger.info("Command workers disabled (CONTROL_PLANE_COMMAND_WORKERS=0)")
        return

    def _loop() -> None:
        global _command_worker_last_error
        last_reconcile = 0.0
        while not _stop_event.wait(timeout=COMMAND_POLL_SECONDS):
            try:
                _work_commands_once()
                if time.monotonic() - last_reconcile >= RECONCILE_SECONDS:
                    last_reconcile = time.monotonic()
                    _reconcile_runs_once()
                    _sweep_orphan_runs()
                _command_worker_last_error = None
            except Exception as exc:  # noqa: BLE001 - a worker must outlive one bad cycle
                _command_worker_last_error = str(exc)
                logger.warning("Command worker cycle failed: %s", exc)

    for n in range(COMMAND_WORKERS):
        thread = threading.Thread(target=_loop, daemon=True, name=f"command-worker-{n}")
        _background_threads.append(thread)
        thread.start()


def _v1_key(context: RequestContext, idempotency_key: str | None, kind: str) -> str:
    if idempotency_key:
        digest = hashlib.sha256(
            f"{context.tenant}\0{context.principal}\0{idempotency_key}".encode()
        ).hexdigest()
        return f"v1:{kind}:{digest[:32]}"
    return f"v1:{kind}:{uuid.uuid4().hex}"


@app.post(
    "/v1/retrain",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandView,
    dependencies=[Depends(_check_rate_limit)],
)
def submit_retrain_v1(
    req: RetrainRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None),
    context: RequestContext = Depends(_require_action("retrain")),
    http_request: Request = None,  # type: ignore[assignment]  # injected by FastAPI
) -> CommandView:
    """Accept a retrain as an asynchronous command: 202 + ``Location`` of its status."""
    _project_gate.enforce_model(context, req.model_name)  # ADR 0014 d4: before any lookup
    _policy_gate.enforce_retrain(req, context, http_request)  # ADR 0079: after the scope check
    registry = _get_registry()
    if req.model_name not in registry:
        raise HTTPException(400, f"Unknown model {req.model_name!r}. Known: {sorted(registry)}")
    if req.dataset_name not in registry[req.model_name]:
        raise HTTPException(
            400,
            f"Dataset {req.dataset_name!r} not supported by {req.model_name}. "
            f"Supported: {registry[req.model_name]}",
        )
    parameters: dict[str, Any] = {
        "model_name": req.model_name,
        "dataset_cls_name": req.dataset_name,
        "is_dummy": req.is_dummy,
        "backend_name": req.backend_name,
        **req.parameters,
    }
    _check_dispatch_parameters(parameters)
    command_key = _v1_key(context, idempotency_key, "retrain")
    view = _submit_command(
        command_key,
        "retrain",
        parameters,
        actor=context.principal,
        tenant=context.tenant,
        exclusive=(req.model_name, req.dataset_name),
        auth=_credential_details(context),
    )
    response.headers["Location"] = view.status_url
    return view


@app.get("/v1/commands/{command_id}", response_model=CommandView)
def get_command_v1(
    command_id: str, context: RequestContext = Depends(_require_read_context)
) -> CommandView:
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT " + _VIEW_COLUMNS + " FROM control_plane_commands "
            "WHERE command_key=? AND tenant=?",
            (command_id, context.tenant),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, f"No command {command_id!r}")
    return _command_view(row)


def _encode_cursor(created_at: str, key: str) -> str:
    import base64  # noqa: PLC0415

    return base64.urlsafe_b64encode(json.dumps([created_at, key]).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    import base64  # noqa: PLC0415

    try:
        created_at, key = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return str(created_at), str(key)
    except Exception as exc:  # noqa: BLE001 - any malformed cursor is the client's error
        raise HTTPException(400, "Malformed cursor") from exc


@app.get("/v1/commands", response_model=CommandPage)
def list_commands_v1(
    state: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    context: RequestContext = Depends(_require_read_context),
) -> CommandPage:
    """Newest first, keyset-paginated: pass ``next_cursor`` back as ``cursor``."""
    limit = max(1, min(limit, 200))
    clauses, params = ["tenant=?"], [context.tenant]
    if state:
        clauses.append("state=?")
        params.append(state)
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    if cursor:
        created_at, key = _decode_cursor(cursor)
        clauses.append("(created_at < ? OR (created_at = ? AND command_key < ?))")
        params += [created_at, created_at, key]
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT "
            + _VIEW_COLUMNS
            + " FROM control_plane_commands WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, command_key DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
    finally:
        conn.close()
    items = [_command_view(r) for r in rows[:limit]]
    next_cursor = (
        _encode_cursor(items[-1].created_at, items[-1].command_id) if len(rows) > limit else None
    )
    return CommandPage(items=items, next_cursor=next_cursor)


@app.delete("/v1/commands/{command_id}", response_model=CommandView)
def cancel_command_v1(
    command_id: str,
    context: RequestContext = Depends(_require_action("retrain")),
) -> CommandView:
    """Cancel an asynchronous command that has not been dispatched yet."""
    _enforce_row_model(context, "command", command_id)  # ADR 0014 d4
    now = datetime.utcnow().isoformat()
    conn = _get_db()
    try:
        conn.execute(begin_immediate("admission"))
        row = conn.execute(
            "SELECT kind, state, mode FROM control_plane_commands WHERE command_key=? AND tenant=?",
            (command_id, context.tenant),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"No command {command_id!r}")
        cancelled = conn.execute(
            "UPDATE control_plane_commands SET state='cancelled', updated_at=? "
            "WHERE command_key=? AND tenant=? AND mode='async' AND state IN ('pending', 'failed')",
            (now, command_id, context.tenant),
        ).rowcount
        if not cancelled:
            raise HTTPException(409, f"Command {command_id!r} is {row[1]} and cannot be cancelled")
        # A cancelled operation is a decision like an approval: published in the same
        # transaction so an agent waiting on it (ADR 0147 d5) can react without polling.
        enqueue_event(
            "operation.cancelled",
            {
                "command_key": command_id,
                "kind": row[0],
                "actor": context.principal,
                "tenant": context.tenant,
            },
            conn=conn,
            actor=context.principal,
            tenant=context.tenant,
        )
        _audit(
            conn,
            context.principal,
            "command_cancelled",
            command_id,
            {"kind": row[0], **_credential_details(context)},
            context.tenant,
        )
        view_row = conn.execute(
            "SELECT " + _VIEW_COLUMNS + " FROM control_plane_commands WHERE command_key=?",
            (command_id,),
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    return _command_view(view_row)


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint() -> Response:
    """Publish the approval gauges from the store, or say the store could not be read.

    Both gauges are derived here rather than carried in memory. The queue depth used to move only
    when an approval event happened in this process, so a restart with a full queue published 0 —
    and ``PendingApprovalQueueLarge`` cannot fire on 0. A failed read used to publish the age
    fallback, which is also 0, and ``ApprovalsStale`` cannot fire on that either. Both alerts were
    therefore guaranteed to stay silent in precisely the states they exist to catch.
    """
    try:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*), MIN(requested_at) FROM pending_approvals WHERE status = 'pending'"
            ).fetchone()
        finally:
            conn.close()
        _metrics.set_pending(int(row[0] or 0))
        _metrics.update_age(row[1])
    except Exception as exc:
        # Leave the gauges holding their last known-true values. Overwriting them here would
        # replace "unknown" with "all clear"; the counter is what makes the failure visible.
        logger.error("metrics_endpoint DB read failed: %s", exc)
        _metrics.record_scrape_error()
    try:
        _metrics.set_outbox(_shared_outbox_stats(), _shared_outbox_oldest_age())
    except Exception as exc:
        logger.error("metrics_endpoint outbox read failed: %s", exc)
        _metrics.record_scrape_error()
    try:
        from examlops.data.audit import dropped_audit_events

        _metrics.publish_dropped_audit_events(dropped_audit_events())
    except Exception as exc:
        logger.error("metrics_endpoint dropped-audit read failed: %s", exc)
        _metrics.record_scrape_error()
    try:
        from examlops.feature_store.scheduler import freshness_report

        _metrics.set_feature_freshness(freshness_report())
    except Exception as exc:
        # Leave the freshness gauges at their last known values; the counter shows the failure
        # (FeatureFreshnessUnreadable).
        logger.error("metrics_endpoint feature-freshness read failed: %s", exc)
        _metrics.record_feature_freshness_read_error()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/modelzoo/status", dependencies=[Depends(_require_read_context)])
def modelzoo_status() -> dict[str, Any]:
    conn = _get_db()
    try:
        freshness_rows = conn.execute(
            "SELECT model_id, latest_modelzoo_commit, last_retrain_commit, "
            "is_stale, stale_since, retrain_triggered_at FROM model_freshness"
        ).fetchall()
        last_event = conn.execute(
            "SELECT commit_sha, timestamp, source FROM modelzoo_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    freshness_map = {row[0]: row for row in freshness_rows}
    registry = _get_registry()
    models = []
    for model_id in registry:
        row = freshness_map.get(model_id)
        if row:
            models.append(
                {
                    "model_id": model_id,
                    "status": "stale" if row[3] else "current",
                    "latest_modelzoo_commit": row[1],
                    "last_retrain_commit": row[2],
                    "stale_since": row[4],
                    "retrain_triggered_at": row[5],
                }
            )
        else:
            models.append(
                {
                    "model_id": model_id,
                    "status": "unknown",
                    "latest_modelzoo_commit": None,
                    "last_retrain_commit": None,
                    "stale_since": None,
                    "retrain_triggered_at": None,
                }
            )

    return {
        "models": models,
        "last_event": {
            "commit_sha": last_event[0],
            "timestamp": last_event[1],
            "source": last_event[2],
        }
        if last_event
        else None,
    }


@app.get("/modelzoo/events", dependencies=[Depends(_require_read_context)])
def modelzoo_events(limit: int = 50) -> list[dict[str, Any]]:
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT id, commit_sha, branch, pushed_by, timestamp, source "
            "FROM modelzoo_events ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r[0],
            "commit_sha": r[1],
            "branch": r[2],
            "pushed_by": r[3],
            "timestamp": r[4],
            "source": r[5],
        }
        for r in rows
    ]


@app.post("/modelzoo/sync")
def modelzoo_sync(
    context: RequestContext = Depends(_require_action("admin")),
    http_request: Request = None,  # type: ignore[assignment]  # injected by FastAPI
) -> dict[str, Any]:
    # ADR 0079: `modelzoo_sync` is the action `exa modelzoo sync` decides as — one rule, two doors.
    _policy_gate.enforce_admin("modelzoo_sync", {"target": "modelzoo"}, context, http_request)
    result = _run_poll_cycle()
    if result.get("error"):
        # Poll could not reach GitLab — report it instead of "up-to-date".
        return {"new_commit": False, "error": result["error"]}
    if not result:
        return {"new_commit": False}
    return {
        "new_commit": True,
        "commit_sha": result.get("commit_sha"),
        "models_marked_stale": len(_get_registry()),
    }


def _modelzoo_settings() -> dict[str, Any]:
    """The ModelZoo settings this replica acts on: the environment's defaults, overlaid by what an
    operator set through the API (shared state, re-read at most every SETTINGS_TTL_SECONDS).

    An unreadable store keeps the last values read rather than falling back to the defaults: a
    database blip must not silently turn auto-retrain back on or off.
    """
    now = time.monotonic()
    with _CONFIG_LOCK:
        if now - _settings_cache["at"] < SETTINGS_TTL_SECONDS:
            return {**_modelzoo_config, **_settings_cache["values"]}
        cached = dict(_settings_cache["values"])
    try:
        conn = _get_db()
        try:
            rows = conn.execute(
                # Bound rather than inlined: see the note in `_sweep_orphan_runs` — a literal
                # `%` becomes a placeholder as soon as this statement takes a parameter.
                "SELECT key, value FROM control_plane_settings WHERE key LIKE ?",
                ("modelzoo.%",),
            ).fetchall()
        finally:
            conn.close()
        values = {str(k).removeprefix("modelzoo."): json.loads(v) for k, v in rows}
    except Exception as exc:  # noqa: BLE001 - keep acting on the last values read
        logger.warning("Could not read shared control-plane settings: %s", exc)
        values = cached
    with _CONFIG_LOCK:
        _settings_cache.update(at=now, values=values)
    return {**_modelzoo_config, **values}


@app.get("/modelzoo/config", dependencies=[Depends(_require_read_context)])
def get_modelzoo_config() -> dict[str, Any]:
    return _modelzoo_settings()


class ModelzooConfigUpdate(BaseModel):
    auto_retrain: bool | None = None
    poll_interval_seconds: int | None = None


@app.put("/modelzoo/config")
def update_modelzoo_config(
    body: ModelzooConfigUpdate,
    context: RequestContext = Depends(_require_action("admin")),
    http_request: Request = None,  # type: ignore[assignment]  # injected by FastAPI
) -> dict[str, Any]:
    """Change the ModelZoo settings for every replica, and record who changed what."""
    changes: dict[str, Any] = {}
    if body.auto_retrain is not None:
        changes["auto_retrain"] = body.auto_retrain
    if body.poll_interval_seconds is not None:
        changes["poll_interval_seconds"] = max(0, body.poll_interval_seconds)
    # ADR 0079: the action `exa modelzoo config-set` decides as.
    _policy_gate.enforce_admin(
        "modelzoo_config_set",
        {"target": "modelzoo", "key": ",".join(sorted(changes)), **changes},
        context,
        http_request,
    )
    if changes:
        now = datetime.utcnow().isoformat()
        conn = _get_db()
        try:
            conn.execute(begin_immediate("settings"))
            for key, value in changes.items():
                conn.execute(
                    "INSERT INTO control_plane_settings (key, value, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                    (f"modelzoo.{key}", json.dumps(value), now, context.principal),
                )
            _audit(
                conn,
                context.principal,
                "modelzoo_config_updated",
                "modelzoo",
                changes,
                context.tenant,
            )
            conn.commit()
        finally:
            conn.close()
        with _CONFIG_LOCK:
            _settings_cache["at"] = float("-inf")  # this replica sees its own change at once
    return _modelzoo_settings()


# ─── Improvement 19: Config hot-reload ───────────────────────────────────────


@app.post("/admin/reload")
def admin_reload(
    context: RequestContext = Depends(_require_action("admin")),
    http_request: Request = None,  # type: ignore[assignment]  # injected by FastAPI
) -> dict[str, Any]:
    """Invalidate the registry cache and re-run startup checks without restarting.

    Use after adding/removing model YAML files or fixing the DB/token configuration.
    """
    # ADR 0079: the action `exa production reload` decides as — one rule, two doors.
    _policy_gate.enforce_admin("production_reload", {"target": "registry"}, context, http_request)
    _invalidate_registry_cache()
    new_registry = _get_registry()
    _run_startup_checks()
    logger.info("Admin reload: registry=%d models, checks=%s", len(new_registry), _startup_checks)
    return {
        "registry_reloaded": True,
        "models": sorted(new_registry.keys()),
        "startup_checks": _startup_checks,
        "dispatch": _dispatch_status(),
    }


# ─── The versioned API (plan P1.6) ───────────────────────────────────────────
# Last, after every route exists: each legacy operator route gets its /v1 twin (the same handler),
# and the legacy paths announce that twin in Deprecation/Link headers. See cplane/versioning.py.
from cplane.versioning import DeprecationHeaders as _DeprecationHeaders  # noqa: E402
from cplane.versioning import install_v1_aliases as _install_v1_aliases  # noqa: E402

_install_v1_aliases(app)
app.add_middleware(_DeprecationHeaders)


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=CONTROL_PLANE_PORT,
        log_level="info",
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
