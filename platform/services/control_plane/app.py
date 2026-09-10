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
    IDEMPOTENCY_TTL_SECONDS           default: 300 (5 min) idempotency cache TTL
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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeVar

import metrics as _metrics
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from examlops.admission import max_running as admission_max_running
from examlops.admission import per_tenant_cap as admission_per_tenant_cap
from examlops.coordination import get_coordinator as _selected_coordinator
from examlops.data.events import enqueue_event
from examlops.data.events import outbox_stats as _shared_outbox_stats
from examlops.events import get_publisher as _selected_publisher
from examlops.events import relay_once as _shared_relay_once
from examlops.storage import PostgresBackend, SqliteBackend

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


def _token_is_usable() -> bool:
    """True only when a real token is configured — not unset and not a known placeholder."""
    return _secret_is_usable(CONTROL_PLANE_TOKEN)


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
            if any(not isinstance(scope, str) for scope in scopes) or not scope_set <= {
                "read",
                "write",
            }:
                raise ValueError("credential scopes must contain only 'read' and/or 'write'")
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


def _auth_is_usable() -> bool:
    return _credential_config_error is None and (
        bool(_structured_credentials) or _token_is_usable()
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
CONTROL_PLANE_STATE_BACKEND = os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower()
CONTROL_PLANE_DB = os.getenv("CONTROL_PLANE_DB") or os.getenv("PLATFORM_DB") or "/data/approvals.db"
# Shared DB-backed coordination and the stock outbox relay resolve SQLite through PLATFORM_DB.
# This service owns one transactional state boundary, so make its explicit database authoritative
# inside this process. Postgres ignores file paths and already converges on DSN + schema.
if CONTROL_PLANE_STATE_BACKEND == "sqlite":
    os.environ["PLATFORM_DB"] = CONTROL_PLANE_DB
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
# Improvement 17
IDEMPOTENCY_TTL_SECONDS = float(os.getenv("IDEMPOTENCY_TTL_SECONDS", "300"))
# A process may disappear after claiming a command but before recording Prefect's response. The
# replacement process may reclaim it after this lease and re-submit with the same Prefect
# idempotency key, which is safe even when the first POST reached Prefect before the crash.
COMMAND_LEASE_SECONDS = max(1, int(os.getenv("CONTROL_PLANE_COMMAND_LEASE_SECONDS", "300")))
RETRAIN_LOCK_SECONDS = max(
    60, int(os.getenv("CONTROL_PLANE_RETRAIN_LOCK_SECONDS", str(COMMAND_LEASE_SECONDS)))
)
POLLER_LEASE_SECONDS = max(3, int(os.getenv("CONTROL_PLANE_POLLER_LEASE_SECONDS", "30")))
EVENT_RELAY_SECONDS = max(0.0, float(os.getenv("CONTROL_PLANE_EVENT_RELAY_SECONDS", "1")))
EVENT_RELAY_BATCH_SIZE = max(1, int(os.getenv("CONTROL_PLANE_EVENT_RELAY_BATCH_SIZE", "100")))
# When set, a modelzoo push event triggers the ai-production CI pipeline automatically.
AI_PROD_PROJECT_ID = os.getenv("AI_PROD_GITLAB_PROJECT_ID", "")
AI_PROD_PIPELINE_TOKEN = os.getenv("AI_PROD_PIPELINE_TRIGGER_TOKEN", "")

_modelzoo_config: dict[str, Any] = {
    "auto_retrain": MODELZOO_AUTO_RETRAIN,
    "poll_interval_seconds": MODELZOO_POLL_SECONDS,
    "watch_branch": MODELZOO_WATCH_BRANCH,
}
_instance_id = uuid.uuid4().hex


# ─── Improvement 14: Request-ID context var ──────────────────────────────────

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


# ─── Shared coordination (rate limits, retrain locks, poller lease) ──────────


def _get_coordinator() -> Any:
    """Return the configured shared coordinator (DB or Redis)."""
    return _selected_coordinator()


# ─── Improvement 12: Prefect circuit breaker ─────────────────────────────────


class _CircuitBreaker:
    """Three-state circuit breaker: CLOSED → OPEN → HALF-OPEN → CLOSED.

    Opens after PREFECT_CB_FAIL_MAX consecutive upstream failures.
    After PREFECT_CB_RESET_TIMEOUT seconds in OPEN state, allows one trial (HALF-OPEN).
    A successful trial closes the breaker; failure re-opens it immediately.
    """

    _CLOSED = "closed"
    _OPEN = "open"
    _HALF_OPEN = "half-open"

    def __init__(self, fail_max: int = 5, reset_timeout: float = 30.0) -> None:
        self._fail_max = fail_max
        self._reset_timeout = reset_timeout
        self._failures = 0
        self._state = self._CLOSED
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def call(self, fn: Callable[[], T]) -> T:
        """Execute fn, tracking failures. Raises 503 when circuit is open."""
        with self._lock:
            if self._state == self._OPEN:
                if time.monotonic() - self._opened_at >= self._reset_timeout:
                    self._state = self._HALF_OPEN
                    logger.info("Prefect circuit breaker HALF-OPEN — probing")
                else:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Prefect circuit breaker open — upstream unavailable, retry later",
                    )

        try:
            result = fn()
            with self._lock:
                if self._state == self._HALF_OPEN:
                    logger.info("Prefect circuit breaker CLOSED — upstream recovered")
                self._state = self._CLOSED
                self._failures = 0
            return result
        except HTTPException as exc:
            if exc.status_code >= 500:
                self._on_failure()
            raise
        except Exception:
            self._on_failure()
            raise

    def _on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._fail_max or self._state == self._HALF_OPEN:
                self._state = self._OPEN
                self._opened_at = time.monotonic()
                logger.error(
                    "Prefect circuit breaker OPENED after %d consecutive failures", self._failures
                )
                _metrics.record_circuit_breaker_open()


_prefect_breaker = _CircuitBreaker(
    fail_max=PREFECT_CB_FAIL_MAX,
    reset_timeout=PREFECT_CB_RESET_TIMEOUT,
)


# ─── Improvement 17: Idempotency cache ───────────────────────────────────────

_idempotency_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_IDEMPOTENCY_LOCK = threading.Lock()


def _check_idempotency(key: str) -> dict[str, Any] | None:
    with _IDEMPOTENCY_LOCK:
        entry = _idempotency_cache.get(key)
        if entry and time.monotonic() < entry[0]:
            return entry[1]
        if entry:
            del _idempotency_cache[key]
    return None


def _store_idempotency(key: str, response: dict[str, Any]) -> None:
    with _IDEMPOTENCY_LOCK:
        now = time.monotonic()
        expired = [k for k, v in _idempotency_cache.items() if now >= v[0]]
        for k in expired:
            del _idempotency_cache[k]
        _idempotency_cache[key] = (now + IDEMPOTENCY_TTL_SECONDS, response)


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

_DB_LOCK = threading.Lock()
_CONFIG_LOCK = threading.Lock()

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pending_approvals (
    id            TEXT PRIMARY KEY,
    model_id      TEXT NOT NULL,
    commit_sha    TEXT,
    commit_msg    TEXT,
    changed_files TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    prefect_run_id TEXT,
    reject_reason  TEXT,
    tenant         TEXT NOT NULL DEFAULT 'default',
    requested_by   TEXT NOT NULL DEFAULT 'legacy',
    resolved_by    TEXT,
    requested_at  TEXT NOT NULL,
    resolved_at   TEXT
)
"""

_CREATE_MODELZOO_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS modelzoo_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_sha TEXT NOT NULL,
    branch     TEXT NOT NULL,
    pushed_by  TEXT,
    timestamp  TEXT NOT NULL,
    source     TEXT NOT NULL,
    raw_payload TEXT
)
"""

_CREATE_MODEL_FRESHNESS_SQL = """
CREATE TABLE IF NOT EXISTS model_freshness (
    model_id               TEXT PRIMARY KEY,
    latest_modelzoo_commit TEXT,
    last_retrain_commit    TEXT,
    is_stale               INTEGER NOT NULL DEFAULT 0,
    stale_since            TEXT,
    retrain_triggered_at   TEXT
)
"""

_CREATE_COMMANDS_SQL = """
CREATE TABLE IF NOT EXISTS control_plane_commands (
    command_key    TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    request_hash   TEXT NOT NULL,
    payload        TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending',
    response       TEXT,
    prefect_run_id TEXT,
    approval_id    TEXT,
    admission_id   INTEGER,
    actor          TEXT NOT NULL DEFAULT 'system',
    tenant         TEXT NOT NULL DEFAULT 'default',
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
)
"""

# These are the same durable queue and transactional-outbox contracts exposed by
# ``examlops.admission`` and ``examlops.events``. They live in the control-plane state database so
# the command, queue transition, approval transition, and emitted event can share one transaction
# on both SQLite and Postgres. ``enqueue_event(..., conn=...)`` below uses the shared outbox helper.
_CREATE_ADMISSION_SQL = """
CREATE TABLE IF NOT EXISTS admission_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant      TEXT NOT NULL DEFAULT 'default',
    project     TEXT,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'queued',
    enqueued_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at  DATETIME,
    finished_at DATETIME,
    reason      TEXT
)
"""

_CREATE_OUTBOX_SQL = """
CREATE TABLE IF NOT EXISTS event_outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    topic        TEXT NOT NULL,
    payload      TEXT NOT NULL,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at DATETIME,
    claimed_at   DATETIME,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    actor        TEXT NOT NULL DEFAULT 'system',
    tenant       TEXT NOT NULL DEFAULT 'default'
)
"""

_SCHEMA_COLUMNS = {
    "pending_approvals": {
        "tenant": "TEXT NOT NULL DEFAULT 'default'",
        "requested_by": "TEXT NOT NULL DEFAULT 'legacy'",
        "resolved_by": "TEXT",
    },
    "control_plane_commands": {
        "actor": "TEXT NOT NULL DEFAULT 'system'",
        "tenant": "TEXT NOT NULL DEFAULT 'default'",
    },
    "event_outbox": {
        "actor": "TEXT NOT NULL DEFAULT 'system'",
        "tenant": "TEXT NOT NULL DEFAULT 'default'",
    },
}

_CREATE_INDICES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_pa_model_status ON pending_approvals(model_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_pa_tenant_model_status "
    "ON pending_approvals(tenant, model_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_me_sha ON modelzoo_events(commit_sha)",
    "CREATE INDEX IF NOT EXISTS idx_cp_commands_state ON control_plane_commands(state, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_admission_state_tenant ON admission_queue(state, tenant, priority, id)",
    "CREATE INDEX IF NOT EXISTS idx_event_outbox_unpublished ON event_outbox(published_at, id)",
]


def _apply_schema_migrations(conn: Any) -> None:
    """Add identity columns without replacing existing SQLite or PostgreSQL tables."""
    for table, definitions in _SCHEMA_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, definition in definitions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _get_db() -> Any:
    """Open the configured control-plane state store.

    SQLite remains the local-development backend and retains the WAL/retry behaviour. Production
    may select the existing shared Postgres adapter with ``EXAMLOPS_DB_BACKEND=postgres`` and
    ``EXAMLOPS_POSTGRES_DSN``. Keeping this behind one connection function lets the endpoint
    contracts remain unchanged while removing the control plane's private persistence island.
    """
    if CONTROL_PLANE_STATE_BACKEND == "postgres":
        conn = PostgresBackend().connect()
        conn.execute(_CREATE_TABLE_SQL)
        conn.execute(_CREATE_MODELZOO_EVENTS_SQL)
        conn.execute(_CREATE_MODEL_FRESHNESS_SQL)
        conn.execute(_CREATE_COMMANDS_SQL)
        conn.execute(_CREATE_ADMISSION_SQL)
        conn.execute(_CREATE_OUTBOX_SQL)
        _apply_schema_migrations(conn)
        for idx_sql in _CREATE_INDICES_SQL:
            conn.execute(idx_sql)
        conn.commit()
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
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_MODELZOO_EVENTS_SQL)
            conn.execute(_CREATE_MODEL_FRESHNESS_SQL)
            conn.execute(_CREATE_COMMANDS_SQL)
            conn.execute(_CREATE_ADMISSION_SQL)
            conn.execute(_CREATE_OUTBOX_SQL)
            _apply_schema_migrations(conn)
            for idx_sql in _CREATE_INDICES_SQL:
                conn.execute(idx_sql)
            conn.commit()
            return conn
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if attempt < 3:
                logger.warning("SQLite open failed (attempt %d/3): %s — retrying", attempt, exc)

    raise sqlite3.OperationalError(f"DB unavailable after 3 attempts: {last_exc}") from last_exc


# ─── Improvement 9 + 19: Startup validation ──────────────────────────────────

_startup_checks: dict[str, str] = {}


def _run_startup_checks() -> None:
    global _startup_checks
    checks: dict[str, str] = {}

    try:
        conn = _get_db()
        conn.execute("SELECT COUNT(*) FROM pending_approvals")
        conn.close()
        checks["db"] = "ok"
    except Exception as exc:
        logger.error("Startup check FAILED — db: %s", exc)
        checks["db"] = f"fail: {exc}"

    try:
        reg = _load_registry()
        checks["registry"] = "ok" if reg else "warn: no enabled models"
        if not reg:
            logger.warning("Startup check WARN — registry: no enabled models found")
    except Exception as exc:
        logger.error("Startup check FAILED — registry: %s", exc)
        checks["registry"] = f"fail: {exc}"

    if _credential_config_error is not None:
        checks["token"] = (
            f"fail: malformed CONTROL_PLANE_CREDENTIALS_JSON: {_credential_config_error}"
        )
        logger.error("Startup check FAILED — credential map is malformed")
    elif _auth_is_usable():
        checks["token"] = "ok"
    elif CONTROL_PLANE_TOKEN.strip():
        checks["token"] = "weak"
        logger.error(
            "Startup check FAILED — CONTROL_PLANE_TOKEN is a known placeholder "
            "(e.g. 'changeme'); protected endpoints return 503 until a real credential is set"
        )
    else:
        checks["token"] = "missing"
        logger.error("Startup check FAILED — no control-plane bearer credential is configured")

    try:
        coordinator = _get_coordinator()
        probe_key = f"control-plane:startup:{_instance_id}"
        if not coordinator.try_lock(probe_key, _instance_id, 5):
            raise RuntimeError("startup coordination probe was not acquired")
        coordinator.unlock(probe_key, _instance_id)
        checks["coordinator"] = "ok"
    except Exception as exc:
        logger.error("Startup check FAILED — coordinator: %s", exc)
        checks["coordinator"] = f"fail: {exc}"

    try:
        _selected_publisher()
        checks["event_publisher"] = "ok"
    except Exception as exc:
        logger.error("Startup check FAILED — event publisher: %s", exc)
        checks["event_publisher"] = f"fail: {exc}"

    _startup_checks = checks
    ok = all(v == "ok" for v in checks.values())
    (logger.info if ok else logger.warning)("Startup checks: %s", checks)
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
    interval = _modelzoo_config.get("poll_interval_seconds", MODELZOO_POLL_SECONDS)
    return (time.time() - _poller_last_ok_ts) > 3 * interval


# ─── Improvement 16: Approval expiry ─────────────────────────────────────────


def _expire_old_approvals() -> int:
    """Mark pending approvals older than APPROVAL_EXPIRY_HOURS as 'expired'."""
    if APPROVAL_EXPIRY_HOURS <= 0:
        return 0
    cutoff = (datetime.utcnow() - timedelta(hours=APPROVAL_EXPIRY_HOURS)).isoformat()
    now = datetime.utcnow().isoformat()
    expired = 0
    with _DB_LOCK:
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


# ─── Schemas ─────────────────────────────────────────────────────────────────


class ChangeNotification(BaseModel):
    model_ids: list[str]
    commit_sha: str | None = None
    commit_msg: str | None = None
    changed_files: list[str] = []


class ApprovalEntry(BaseModel):
    id: str
    model_id: str
    commit_sha: str | None
    commit_msg: str | None
    changed_files: list[str]
    status: str
    prefect_run_id: str | None
    reject_reason: str | None
    tenant: str
    requested_by: str
    resolved_by: str | None
    requested_at: str
    resolved_at: str | None


class RejectRequest(BaseModel):
    reason: str | None = None


class RetrainRequest(BaseModel):
    model_name: str = Field(..., description="Registered model name (e.g. 'JPCP')")
    dataset_name: str = Field(..., description="Dataset class name (e.g. 'PM100Dataset')")
    backend_name: str | None = Field(default=None)
    is_dummy: bool = Field(default=False)
    parameters: dict[str, Any] = Field(default_factory=dict)


class RetrainResponse(BaseModel):
    flow_run_id: str
    deployment: str
    status_url: str
    parameters: dict[str, Any]


class FlowRunStatus(BaseModel):
    flow_run_id: str
    state_type: str | None
    state_name: str | None
    is_terminal: bool


class ModelEntry(BaseModel):
    model_name: str
    datasets: list[str]


# ─── Improvement 8 + 19: Lifespan ────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    _run_startup_checks()
    _expire_old_approvals()
    _get_registry()
    _stop_event.clear()
    _start_poller()
    _start_event_relay()
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


def _request_context(authorization: str | None = Header(default=None)) -> RequestContext:
    """Authenticate one configured bearer credential and return its trusted identity context."""
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
        matched = RequestContext("legacy", "default", frozenset({"read", "write"}), is_legacy=True)
    if matched is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid bearer token")
    return matched


def _require_scope(required: str) -> Callable[[RequestContext], RequestContext]:
    def dependency(context: RequestContext = Depends(_request_context)) -> RequestContext:
        if required not in context.scopes:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing {required!r} scope")
        return context

    return dependency


_require_read_context = _require_scope("read")
_require_write_context = _require_scope("write")


def _require_token(authorization: str | None = Header(default=None)) -> RequestContext:
    """Compatibility alias for older direct callers; write routes use scoped dependencies."""
    return _request_context(authorization)


def _check_rate_limit(
    context: RequestContext = Depends(_require_write_context),
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

    with _DB_LOCK:
        conn = _get_db()
        try:
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
    if _modelzoo_config.get("auto_retrain") and registry:
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
        deployment_id = gateway.find_deployment_id(PREFECT_DEPLOYMENT_NAME)
        flow_run_id = gateway.create_flow_run(
            deployment_id, parameters, idempotency_key=command_key
        )
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
    with _DB_LOCK:
        conn = _get_db()
        try:
            existing = conn.execute(
                "SELECT id FROM modelzoo_events WHERE commit_sha = ?", (latest_sha,)
            ).fetchone()
            if existing:
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

    if _modelzoo_config.get("auto_retrain") and registry:
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
    interval = max(1, int(_modelzoo_config.get("poll_interval_seconds", MODELZOO_POLL_SECONDS)))
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
            _modelzoo_config.get("poll_interval_seconds", MODELZOO_POLL_SECONDS),
        )
        try:
            while not _stop_event.wait(
                timeout=_modelzoo_config.get("poll_interval_seconds", MODELZOO_POLL_SECONDS)
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


def _start_event_relay() -> None:
    if EVENT_RELAY_SECONDS <= 0:
        logger.info("Event relay disabled (CONTROL_PLANE_EVENT_RELAY_SECONDS=0)")
        return

    def _loop() -> None:
        global _relay_last_error, _relay_last_result
        logger.info("Event relay started (interval=%.1fs)", EVENT_RELAY_SECONDS)
        while not _stop_event.wait(timeout=EVENT_RELAY_SECONDS):
            try:
                _relay_last_result = _relay_outbox_once()
                failed = _relay_last_result.get("failed", 0)
                _relay_last_error = f"{failed} event(s) failed to publish" if failed else None
            except Exception as exc:
                _relay_last_error = str(exc)
                logger.warning("Event relay cycle failed: %s", exc)
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


# ─── Prefect client (improvement 3 retry + improvement 12 circuit breaker) ───


class PrefectGateway:
    """urllib-based Prefect REST client with retry + circuit breaker."""

    _RETRY_DELAYS = (0.5, 1.0, 2.0)

    def __init__(self, api_url: str = PREFECT_API_URL) -> None:
        self.api_url = api_url.rstrip("/")

    def get_deployment(self, deployment_name: str) -> dict[str, Any]:
        """The Prefect deployment document, or a 503 that says how to create it.

        A missing dispatch target is a platform misconfiguration, not a missing *API route*: it
        used to surface as Prefect's bare 404, which every caller read as "this endpoint does not
        exist" (finding B2).
        """
        import urllib.parse  # noqa: PLC0415

        if "/" not in deployment_name:
            raise HTTPException(400, f"deployment must be 'flow/name', got {deployment_name!r}")
        flow_name, dep_name = deployment_name.split("/", 1)
        url = (
            f"{self.api_url}/deployments/name/"
            f"{urllib.parse.quote(flow_name)}/{urllib.parse.quote(dep_name)}"
        )
        try:
            payload = _prefect_breaker.call(lambda: self._get(url))
        except HTTPException as exc:
            if exc.status_code == 404:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    f"Prefect deployment {deployment_name!r} does not exist. Register and serve "
                    "it with `exa pipeline deploy` (or set PREFECT_DEPLOYMENT_NAME).",
                ) from exc
            raise
        if not payload.get("id"):
            raise HTTPException(502, f"Prefect returned no id for {deployment_name!r}")
        return payload

    def find_deployment_id(self, deployment_name: str) -> str:
        return str(self.get_deployment(deployment_name)["id"])

    def create_flow_run(
        self,
        deployment_id: str,
        parameters: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> str:
        url = f"{self.api_url}/deployments/{deployment_id}/create_flow_run"
        body: dict[str, Any] = {"parameters": parameters}
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        payload = _prefect_breaker.call(lambda: self._post(url, body))
        run_id = payload.get("id")
        if not run_id:
            raise HTTPException(502, "Prefect create_flow_run returned no id")
        return run_id

    def get_flow_run(self, flow_run_id: str) -> dict[str, Any]:
        return _prefect_breaker.call(lambda: self._get(f"{self.api_url}/flow_runs/{flow_run_id}"))

    def _get(self, url: str) -> dict[str, Any]:
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        last_exc: Exception | None = None
        for attempt, delay in enumerate(self._RETRY_DELAYS, start=1):
            try:
                with urllib.request.urlopen(req, timeout=10.0) as resp:  # noqa: S310
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    raise HTTPException(exc.code, f"Prefect GET {url} -> HTTP {exc.code}") from exc
                last_exc = exc
                _metrics.record_prefect_retry("GET")
                logger.warning(
                    "Prefect GET %s -> %d (attempt %d), retry in %.1fs",
                    url,
                    exc.code,
                    attempt,
                    delay,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                _metrics.record_prefect_retry("GET")
                logger.warning(
                    "Prefect GET %s error (attempt %d): %s, retry in %.1fs",
                    url,
                    attempt,
                    exc,
                    delay,
                )
            time.sleep(delay)
        raise HTTPException(502, f"Prefect unreachable after retries: {last_exc}") from last_exc

    def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        data = json.dumps(body).encode("utf-8")
        last_exc: Exception | None = None
        for attempt, delay in enumerate(self._RETRY_DELAYS, start=1):
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10.0) as resp:  # noqa: S310
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    raise HTTPException(exc.code, f"Prefect POST {url} -> HTTP {exc.code}") from exc
                last_exc = exc
                _metrics.record_prefect_retry("POST")
                logger.warning(
                    "Prefect POST %s -> %d (attempt %d), retry in %.1fs",
                    url,
                    exc.code,
                    attempt,
                    delay,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                _metrics.record_prefect_retry("POST")
                logger.warning(
                    "Prefect POST %s error (attempt %d): %s, retry in %.1fs",
                    url,
                    attempt,
                    exc,
                    delay,
                )
            time.sleep(delay)
        raise HTTPException(502, f"Prefect unreachable after retries: {last_exc}") from last_exc


_gateway: PrefectGateway | None = None


def _get_gateway() -> PrefectGateway:
    global _gateway
    if _gateway is None:
        _gateway = PrefectGateway()
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
    with _DB_LOCK:
        conn = _get_db()
        try:
            # Serialize the read-modify-write claim across processes. The shared Postgres adapter
            # translates this to a transaction-scoped advisory lock.
            conn.execute("BEGIN IMMEDIATE")
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
) -> None:
    """Commit command success, queue completion, approval state, and outbox event atomically."""
    now = datetime.utcnow().isoformat()
    with _DB_LOCK:
        conn = _get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
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
            event_id = enqueue_event(
                event_topic,
                {"command_key": command_key, **event_payload, "actor": actor, "tenant": tenant},
                conn=conn,
            )
            conn.execute(
                "UPDATE event_outbox SET actor=?, tenant=? WHERE id=?",
                (actor, tenant, event_id),
            )
            conn.commit()
        finally:
            conn.close()


def _fail_command(
    command_key: str, exc: Exception, *, attempt: int, approval_id: str | None = None
) -> None:
    """Persist a retryable command failure and release an approval claim."""
    now = datetime.utcnow().isoformat()
    with _DB_LOCK:
        conn = _get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
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
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
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
    blockers = ["retrain_dedup_requires_client_key", "circuit_breaker_process_local"]
    if CONTROL_PLANE_STATE_BACKEND != "postgres":
        blockers.append("state_not_shared")
    if coordinator_name == "db" and CONTROL_PLANE_STATE_BACKEND != "postgres":
        blockers.append("coordination_not_cross_host")
    if EVENT_RELAY_SECONDS <= 0:
        blockers.append("event_relay_disabled")
    if publisher_name == "log":
        blockers.append("event_publisher_process_local")
    elif publisher_name in {"nats", "kafka"}:
        blockers.append("event_publisher_not_implemented")
    try:
        outbox: dict[str, Any] = _shared_outbox_stats()
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
        "horizontal_scaling_safe": False,
        "horizontal_scaling_blockers": blockers,
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
    # Readiness is decided before the dispatch verdict is folded in. A retrain target that is not
    # deployed yet makes every retrain fail, so `status` must not say "ok" — but it is not a reason
    # to pull the API out of rotation: approvals, reads and the durable command record still work,
    # and on a fresh stack the control plane legitimately starts before `exa pipeline deploy` runs.
    ready = status == "ok" and not (poller_enabled and _is_poller_stale())
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
    context: RequestContext = Depends(_require_write_context),
) -> RetrainResponse:
    """Improvements 5 (dedup), 13 (metrics), 17 (idempotency), 12 (circuit breaker)."""
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
        deployment_id = gateway.find_deployment_id(PREFECT_DEPLOYMENT_NAME)
        flow_run_id = gateway.create_flow_run(
            deployment_id, parameters, idempotency_key=external_key
        )
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
        payload = await request.json()
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
    raw_body = await request.body()
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
        "seanerbus_uuid": m.seanerbus_uuid,
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
    context: RequestContext = Depends(_require_write_context),
) -> dict[str, Any]:
    created: list[str] = []
    created_model_ids: list[str] = []
    now = datetime.utcnow().isoformat()
    changed_files_json = json.dumps(notification.changed_files)

    with _DB_LOCK:
        conn = _get_db()
        try:
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
    context: RequestContext = Depends(_require_write_context),
) -> dict[str, Any]:
    # Improvement 16: reject expired entries before approving
    _expire_old_approvals()

    # Find either a new approval or a previously interrupted dispatch. The durable command lease
    # below decides whether an ``approving`` row is still owned or is safe to recover.
    with _DB_LOCK:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT id, status FROM pending_approvals "
                "WHERE tenant = ? AND model_id = ? AND status IN ('pending', 'approving') "
                "ORDER BY requested_at DESC LIMIT 1",
                (context.tenant, model_id),
            ).fetchone()
            if not row:
                raise HTTPException(404, f"No pending approval found for model {model_id!r}")
            row_id = row[0]
            approval_status = row[1]
        finally:
            conn.close()

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
        deployment_id = gateway.find_deployment_id(PREFECT_DEPLOYMENT_NAME)
        flow_run_id: str = gateway.create_flow_run(
            deployment_id, parameters, idempotency_key=command_key
        )
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
    context: RequestContext = Depends(_require_write_context),
) -> dict[str, Any]:
    pending_count = 0
    with _DB_LOCK:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT id FROM pending_approvals "
                "WHERE tenant = ? AND model_id = ? AND status = 'pending' "
                "ORDER BY requested_at DESC LIMIT 1",
                (context.tenant, model_id),
            ).fetchone()
            if not row:
                raise HTTPException(404, f"No pending approval found for model {model_id!r}")
            row_id = row[0]
            conn.execute(
                "UPDATE pending_approvals SET status='rejected', reject_reason=?, resolved_by=?, "
                "resolved_at=? WHERE id=?",
                (body.reason, context.principal, datetime.utcnow().isoformat(), row_id),
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


@app.delete(
    "/approvals/{approval_id}",
    dependencies=[Depends(_check_rate_limit)],
)
def retract_approval(
    approval_id: str,
    context: RequestContext = Depends(_require_write_context),
) -> dict[str, Any]:
    """Retract a pending approval (a stale or duplicate entry) without erasing it.

    `exa approvals delete` called this route for a long time before it existed (plan P0.3 /
    finding B3). It is a *retraction*, not a delete: an approval is a governance record, so the
    row stays, marked ``retracted`` with who and when, and an ``approval.retracted`` event goes to
    the outbox in the same transaction. Only a ``pending`` approval in the caller's tenant can be
    retracted; one being dispatched (``approving``) or already resolved answers 409.
    """
    now = datetime.utcnow().isoformat()
    with _DB_LOCK:
        conn = _get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
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
            event_id = enqueue_event(
                "approval.retracted",
                {
                    "approval_id": approval_id,
                    "model_id": model_id,
                    "actor": context.principal,
                    "tenant": context.tenant,
                },
                conn=conn,
            )
            conn.execute(
                "UPDATE event_outbox SET actor=?, tenant=? WHERE id=?",
                (context.principal, context.tenant, event_id),
            )
            conn.commit()
        finally:
            conn.close()
    logger.info("Retracted approval id=%s model=%s by=%s", approval_id, model_id, context.principal)
    return {"id": approval_id, "model_id": model_id, "status": "retracted"}


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
        with _DB_LOCK:
            conn = _get_db()
            try:
                row = conn.execute(
                    "SELECT COUNT(*), MIN(requested_at) FROM pending_approvals "
                    "WHERE status = 'pending'"
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


@app.post("/modelzoo/sync", dependencies=[Depends(_require_write_context)])
def modelzoo_sync() -> dict[str, Any]:
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


@app.get("/modelzoo/config", dependencies=[Depends(_require_read_context)])
def get_modelzoo_config() -> dict[str, Any]:
    with _CONFIG_LOCK:
        return dict(_modelzoo_config)


class ModelzooConfigUpdate(BaseModel):
    auto_retrain: bool | None = None
    poll_interval_seconds: int | None = None


@app.put("/modelzoo/config", dependencies=[Depends(_require_write_context)])
def update_modelzoo_config(body: ModelzooConfigUpdate) -> dict[str, Any]:
    with _CONFIG_LOCK:
        if body.auto_retrain is not None:
            _modelzoo_config["auto_retrain"] = body.auto_retrain
        if body.poll_interval_seconds is not None:
            _modelzoo_config["poll_interval_seconds"] = max(0, body.poll_interval_seconds)
        return dict(_modelzoo_config)


# ─── Improvement 19: Config hot-reload ───────────────────────────────────────


@app.post("/admin/reload", dependencies=[Depends(_require_write_context)])
def admin_reload() -> dict[str, Any]:
    """Invalidate the registry cache and re-run startup checks without restarting.

    Use after adding/removing model YAML files or fixing the DB/token configuration.
    """
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
