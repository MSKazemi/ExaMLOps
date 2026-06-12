"""
ExaMLOps Control Plane — retraining trigger API.

Reliability improvements batch 1 (2026-06-12 round 1):
  1.  Registry TTL cache (60 s) — eliminates per-request YAML disk reads
  2.  SQLite WAL mode + indices — better concurrency and faster hot queries
  3.  Prefect gateway retry (3 attempts, exponential backoff) — tolerates blips
  4.  Concurrent /status pings — worst-case 5 s instead of 25 s
  5.  Retrain deduplication — 409 on concurrent same-model/dataset requests
  6.  Write-endpoint rate limiting — token bucket, 429 with Retry-After
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
    CONTROL_PLANE_TOKEN               REQUIRED — bearer token for write endpoints
    PREFECT_API_URL                   default: http://localhost:4200/api
    PREFECT_DEPLOYMENT_NAME           default: examlops_scheduled_training/nightly
    CONTROL_PLANE_DB                  default: /data/approvals.db
    RETRAIN_RATE_LIMIT_PER_MIN        default: 20
    LOG_FORMAT                        default: text  (set to "json" in production)
    APPROVAL_EXPIRY_HOURS             default: 72  (0 = disabled)
    PREFECT_CB_FAIL_MAX               default: 5   circuit breaker open threshold
    PREFECT_CB_RESET_TIMEOUT          default: 30  seconds before half-open retry
    IDEMPOTENCY_TTL_SECONDS           default: 300 (5 min) idempotency cache TTL
"""

from __future__ import annotations

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

T = TypeVar("T")

# ─── Improvement 11: Structured JSON logging ─────────────────────────────────

_LOG_FORMAT = os.getenv("LOG_FORMAT", "text").lower()


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object for Loki ingestion."""

    _SKIP = frozenset((
        "args", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelno", "lineno", "message", "module", "msecs", "msg", "name",
        "pathname", "process", "processName", "relativeCreated",
        "stack_info", "thread", "threadName",
    ))

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
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [control-plane] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
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
PREFECT_API_URL = os.getenv("PREFECT_API_URL", "http://localhost:4200/api").rstrip("/")
PREFECT_DEPLOYMENT_NAME = os.getenv(
    "PREFECT_DEPLOYMENT_NAME", "examlops_scheduled_training/nightly"
)
CONTROL_PLANE_DB = os.getenv("CONTROL_PLANE_DB", "/data/approvals.db")
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

_modelzoo_config: dict[str, Any] = {
    "auto_retrain": MODELZOO_AUTO_RETRAIN,
    "poll_interval_seconds": MODELZOO_POLL_SECONDS,
    "watch_branch": MODELZOO_WATCH_BRANCH,
}


# ─── Improvement 14: Request-ID context var ──────────────────────────────────

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


# ─── Rate limiter (improvement 6) ────────────────────────────────────────────

class _TokenBucket:
    def __init__(self, capacity: int, refill_rate: float) -> None:
        self._capacity = float(capacity)
        self._refill_rate = refill_rate
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, n: float = 1.0) -> bool:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._last_refill) * self._refill_rate)
            self._last_refill = now
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False


_rate_limiter = _TokenBucket(
    capacity=RETRAIN_RATE_LIMIT_PER_MIN,
    refill_rate=RETRAIN_RATE_LIMIT_PER_MIN / 60.0,
)


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

_CREATE_INDICES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_pa_model_status ON pending_approvals(model_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_me_sha ON modelzoo_events(commit_sha)",
]


def _get_db() -> sqlite3.Connection:
    """Open the SQLite DB.

    Improvement 2: WAL mode for better concurrency.
    Improvement 20: Retries on OperationalError for NFS-hosted DB resilience.
    """
    db_path = CONTROL_PLANE_DB
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except OSError:
            db_path = "./approvals.db"

    last_exc: Exception | None = None
    for attempt, delay in enumerate([0.0, 0.1, 0.3], start=1):
        if delay:
            time.sleep(delay)
        try:
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_MODELZOO_EVENTS_SQL)
            conn.execute(_CREATE_MODEL_FRESHNESS_SQL)
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

    checks["token"] = "ok" if CONTROL_PLANE_TOKEN else "missing"
    if not CONTROL_PLANE_TOKEN:
        logger.error("Startup check FAILED — CONTROL_PLANE_TOKEN unset; POST /retrain returns 503")

    _startup_checks = checks
    ok = all(v == "ok" for v in checks.values())
    (logger.info if ok else logger.warning)("Startup checks: %s", checks)


# ─── Improvement 7 + 8: Poller state ─────────────────────────────────────────

_poller_last_ok_ts: float = 0.0
_stop_event = threading.Event()


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
        logger.info("Expired %d stale pending approval(s) (>%dh old)", expired, APPROVAL_EXPIRY_HOURS)
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
    yield
    _stop_event.set()
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
        response.headers.update({
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "X-XSS-Protection": "1; mode=block",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        })
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


# ─── Auth & rate limiting ─────────────────────────────────────────────────────


def _require_token(authorization: str | None = Header(default=None)) -> None:
    if not CONTROL_PLANE_TOKEN:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Control plane not configured (CONTROL_PLANE_TOKEN unset)")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    if authorization.removeprefix("Bearer ").strip() != CONTROL_PLANE_TOKEN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid bearer token")


def _check_rate_limit() -> None:
    if not _rate_limiter.consume():
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded",
            headers={"Retry-After": "60"},
        )


# ─── ModelZoo webhook auth ────────────────────────────────────────────────────


def _verify_gitlab_token(x_gitlab_token: str | None) -> None:
    if not MODELZOO_WEBHOOK_SECRET:
        raise HTTPException(503, "MODELZOO_WEBHOOK_SECRET not configured")
    if not x_gitlab_token or x_gitlab_token != MODELZOO_WEBHOOK_SECRET:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid X-Gitlab-Token")


def _verify_github_signature(raw_body: bytes, x_hub_signature_256: str | None) -> None:
    if not MODELZOO_WEBHOOK_SECRET:
        raise HTTPException(503, "MODELZOO_WEBHOOK_SECRET not configured")
    if not x_hub_signature_256:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-Hub-Signature-256")
    expected = "sha256=" + _hmac.new(
        MODELZOO_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    if not _hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid X-Hub-Signature-256")


def _record_push_event(commit_sha: str, branch: str, pushed_by: str, raw_payload: str) -> dict[str, Any]:
    now = datetime.utcnow().isoformat()
    registry = _get_registry()
    event_id: int = 0

    with _DB_LOCK:
        conn = _get_db()
        try:
            conn.execute(
                "INSERT INTO modelzoo_events (commit_sha, branch, pushed_by, timestamp, source, raw_payload) "
                "VALUES (?, ?, ?, ?, 'webhook', ?)",
                (commit_sha, branch, pushed_by, now, raw_payload),
            )
            event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
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

    return {"event_id": event_id, "models_marked_stale": len(registry), "retrain_triggered": retrain_triggered}


def _auto_retrain_model(model_id: str, dataset_name: str, commit_sha: str) -> None:
    gateway = _get_gateway()
    deployment_id = gateway.find_deployment_id(PREFECT_DEPLOYMENT_NAME)
    flow_run_id = gateway.create_flow_run(
        deployment_id,
        {"model_name": model_id, "dataset_cls_name": dataset_name, "is_dummy": False, "backend_name": None},
    )
    now = datetime.utcnow().isoformat()
    with _DB_LOCK:
        conn = _get_db()
        try:
            conn.execute(
                "INSERT INTO model_freshness (model_id, latest_modelzoo_commit, last_retrain_commit, is_stale, retrain_triggered_at) "
                "VALUES (?, ?, ?, 0, ?) ON CONFLICT(model_id) DO UPDATE SET "
                "last_retrain_commit=excluded.latest_modelzoo_commit, is_stale=0, retrain_triggered_at=excluded.retrain_triggered_at",
                (model_id, commit_sha, commit_sha, now),
            )
            conn.commit()
        finally:
            conn.close()
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
        logger.warning("ModelZoo poll failed: %s", exc)
        return {}

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
            conn.execute(
                "INSERT INTO modelzoo_events (commit_sha, branch, pushed_by, timestamp, source) VALUES (?, ?, ?, ?, 'poll')",
                (latest_sha, MODELZOO_WATCH_BRANCH, pushed_by, committed_at),
            )
            event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
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

    return {"new": True, "commit_sha": latest_sha, "event_id": event_id}


def _start_poller() -> None:
    if MODELZOO_POLL_SECONDS <= 0:
        logger.info("ModelZoo poller disabled (MODELZOO_POLL_SECONDS=0)")
        return

    def _loop() -> None:
        global _poller_last_ok_ts
        logger.info("ModelZoo poller started (interval=%ds)",
                    _modelzoo_config.get("poll_interval_seconds", MODELZOO_POLL_SECONDS))
        while not _stop_event.wait(
            timeout=_modelzoo_config.get("poll_interval_seconds", MODELZOO_POLL_SECONDS)
        ):
            try:
                _run_poll_cycle()
                _expire_old_approvals()
                _poller_last_ok_ts = time.time()
            except Exception as exc:
                logger.warning("Poll cycle error: %s", exc)
        logger.info("ModelZoo poller stopped")

    threading.Thread(target=_loop, daemon=True, name="modelzoo-poller").start()


# ─── Registry helpers ─────────────────────────────────────────────────────────


def _load_registry() -> dict[str, list[str]]:
    import yaml  # noqa: PLC0415

    result: dict[str, list[str]] = {}
    try:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        models_dir = os.path.join(repo_root, "pipelines", "models")
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

    def find_deployment_id(self, deployment_name: str) -> str:
        import urllib.parse  # noqa: PLC0415

        if "/" not in deployment_name:
            raise HTTPException(400, f"deployment must be 'flow/name', got {deployment_name!r}")
        flow_name, dep_name = deployment_name.split("/", 1)
        url = (
            f"{self.api_url}/deployments/name/"
            f"{urllib.parse.quote(flow_name)}/{urllib.parse.quote(dep_name)}"
        )
        payload = _prefect_breaker.call(lambda: self._get(url))
        dep_id = payload.get("id")
        if not dep_id:
            raise HTTPException(502, f"Prefect returned no id for {deployment_name!r}")
        return dep_id

    def create_flow_run(self, deployment_id: str, parameters: dict[str, Any]) -> str:
        url = f"{self.api_url}/deployments/{deployment_id}/create_flow_run"
        payload = _prefect_breaker.call(lambda: self._post(url, {"parameters": parameters}))
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
                logger.warning("Prefect GET %s -> %d (attempt %d), retry in %.1fs", url, exc.code, attempt, delay)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                _metrics.record_prefect_retry("GET")
                logger.warning("Prefect GET %s error (attempt %d): %s, retry in %.1fs", url, attempt, exc, delay)
            time.sleep(delay)
        raise HTTPException(502, f"Prefect unreachable after retries: {last_exc}") from last_exc

    def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        data = json.dumps(body).encode("utf-8")
        last_exc: Exception | None = None
        for attempt, delay in enumerate(self._RETRY_DELAYS, start=1):
            req = urllib.request.Request(
                url, data=data,
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
                logger.warning("Prefect POST %s -> %d (attempt %d), retry in %.1fs", url, exc.code, attempt, delay)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                _metrics.record_prefect_retry("POST")
                logger.warning("Prefect POST %s error (attempt %d): %s, retry in %.1fs", url, attempt, exc, delay)
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


# ─── Endpoints ───────────────────────────────────────────────────────────────


@app.get("/ready", include_in_schema=False)
def ready() -> dict[str, str]:
    """Improvement 15: fast liveness probe — always 200 if the process is alive."""
    return {"status": "alive"}


@app.get("/health")
def health() -> dict[str, Any]:
    pending_count = 0
    conn = None
    try:
        conn = _get_db()
        row = conn.execute("SELECT COUNT(*) FROM pending_approvals WHERE status = 'pending'").fetchone()
        pending_count = row[0] if row else 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not query pending approvals count: %s", exc)
    finally:
        if conn:
            conn.close()

    poller_enabled = MODELZOO_POLL_SECONDS > 0
    poller_info: dict[str, Any] = {"enabled": poller_enabled}
    if poller_enabled:
        poller_info["last_ok_seconds_ago"] = int(time.time() - _poller_last_ok_ts) if _poller_last_ok_ts else None
        poller_info["stale"] = _is_poller_stale()

    all_ok = all(v == "ok" for v in _startup_checks.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "prefect_api_url": PREFECT_API_URL,
        "deployment": PREFECT_DEPLOYMENT_NAME,
        "auth_configured": bool(CONTROL_PLANE_TOKEN),
        "models": _get_registry(),
        "pending_approvals": pending_count,
        "startup_checks": _startup_checks,
        "poller": poller_info,
        "circuit_breaker": {"state": _prefect_breaker.state},
    }


@app.get("/status")
def platform_status() -> dict[str, Any]:
    """Improvement 4: all pings concurrent via ThreadPoolExecutor."""
    import urllib.request  # noqa: PLC0415

    def _ping(url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=5.0) as r:  # noqa: S310
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

    pending_count = 0
    conn = None
    try:
        conn = _get_db()
        row = conn.execute("SELECT COUNT(*) FROM pending_approvals WHERE status = 'pending'").fetchone()
        pending_count = row[0] if row else 0
    except Exception:  # noqa: BLE001
        pass
    finally:
        if conn:
            conn.close()

    with ThreadPoolExecutor(max_workers=5) as pool:
        f_mlflow = pool.submit(_ping, f"{MLFLOW_URL}/health")
        f_prefect = pool.submit(_ping, f"{PREFECT_API_URL}/health")
        f_ray = pool.submit(_ping_json, f"{RAY_SERVE_URL}/models")
        f_dashboard = pool.submit(_ping, f"{DASHBOARD_URL}/api/health")
        mlflow_ok = f_mlflow.result()
        prefect_ok = f_prefect.result()
        ray_ok, ray_data = f_ray.result()
        dashboard_ok = f_dashboard.result()

    ray_models: list[str] = (
        [m.get("name", m) if isinstance(m, dict) else m for m in (ray_data or [])] if ray_ok else []
    )
    return {
        "services": {
            "control_plane": {"ok": True},
            "mlflow": {"ok": mlflow_ok},
            "prefect": {"ok": prefect_ok},
            "ray_serve": {"ok": ray_ok, "models": ray_models},
            "dashboard": {"ok": dashboard_ok},
        },
        "pending_approvals": pending_count,
    }


@app.get("/models", response_model=list[ModelEntry])
def list_models() -> list[ModelEntry]:
    return [ModelEntry(model_name=n, datasets=d) for n, d in _get_registry().items()]


@app.post(
    "/retrain",
    response_model=RetrainResponse,
    dependencies=[Depends(_require_token), Depends(_check_rate_limit)],
)
def trigger_retrain(
    req: RetrainRequest,
    x_idempotency_key: str | None = Header(default=None),
) -> RetrainResponse:
    """Improvements 5 (dedup), 13 (metrics), 17 (idempotency), 12 (circuit breaker)."""
    # Improvement 17: idempotency
    if x_idempotency_key:
        cached = _check_idempotency(x_idempotency_key)
        if cached:
            logger.info("Returning cached response for idempotency key %s", x_idempotency_key)
            return RetrainResponse(**cached)

    registry = _get_registry()
    if req.model_name not in registry:
        raise HTTPException(400, f"Unknown model {req.model_name!r}. Known: {sorted(registry)}")
    if req.dataset_name not in registry[req.model_name]:
        raise HTTPException(
            400,
            f"Dataset {req.dataset_name!r} not supported by {req.model_name}. "
            f"Supported: {registry[req.model_name]}",
        )

    # Improvement 5: deduplication
    key = _retrain_key(req.model_name, req.dataset_name)
    with _RETRAIN_LOCK:
        if key in _inflight_retrains:
            _metrics.record_retrain(req.model_name, req.dataset_name, "dedup")
            raise HTTPException(409, f"Retrain already in-flight for {key}")
        _inflight_retrains.add(key)

    parameters: dict[str, Any] = {
        "model_name": req.model_name,
        "dataset_cls_name": req.dataset_name,
        "is_dummy": req.is_dummy,
        "backend_name": req.backend_name,
        **req.parameters,
    }

    # Improvement 13: retrain metrics
    start_ts = time.monotonic()
    try:
        gateway = _get_gateway()
        deployment_id = gateway.find_deployment_id(PREFECT_DEPLOYMENT_NAME)
        flow_run_id = gateway.create_flow_run(deployment_id, parameters)
    except Exception:
        _metrics.record_retrain(req.model_name, req.dataset_name, "error")
        raise
    finally:
        with _RETRAIN_LOCK:
            _inflight_retrains.discard(key)
        duration = time.monotonic() - start_ts
        _metrics.observe_retrain_duration(req.model_name, req.dataset_name, duration)

    _metrics.record_retrain(req.model_name, req.dataset_name, "success")
    logger.info(
        "Scheduled retrain model=%s dataset=%s flow_run_id=%s",
        req.model_name, req.dataset_name, flow_run_id,
    )

    response_data = {
        "flow_run_id": flow_run_id,
        "deployment": PREFECT_DEPLOYMENT_NAME,
        "status_url": f"/retrain/{flow_run_id}",
        "parameters": parameters,
    }
    if x_idempotency_key:
        _store_idempotency(x_idempotency_key, response_data)

    return RetrainResponse(**response_data)


@app.get("/retrain/{flow_run_id}", response_model=FlowRunStatus)
def retrain_status(flow_run_id: str) -> FlowRunStatus:
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
    payload = await request.json()
    ref = payload.get("ref", "")
    if not ref.endswith(f"/{MODELZOO_WATCH_BRANCH}"):
        return {"skipped": True, "reason": f"branch {ref!r} is not {MODELZOO_WATCH_BRANCH!r}"}
    commits = payload.get("commits", [])
    commit_sha = commits[0]["id"] if commits else payload.get("after", "")
    if not commit_sha:
        return {"skipped": True, "reason": "no commit SHA in payload"}
    pushed_by = payload.get("user_name") or (commits[0].get("author", {}).get("name") if commits else "unknown")
    return _record_push_event(commit_sha, MODELZOO_WATCH_BRANCH, pushed_by or "unknown", json.dumps(payload))


@app.post("/webhooks/modelzoo/github")
async def webhook_github(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    _verify_github_signature(raw_body, request.headers.get("x-hub-signature-256"))
    payload = json.loads(raw_body)
    ref = payload.get("ref", "")
    if not ref.endswith(f"/{MODELZOO_WATCH_BRANCH}"):
        return {"skipped": True, "reason": f"branch {ref!r} is not {MODELZOO_WATCH_BRANCH!r}"}
    head = payload.get("head_commit") or {}
    commit_sha = head.get("id", payload.get("after", ""))
    if not commit_sha:
        return {"skipped": True, "reason": "no commit SHA in payload"}
    return _record_push_event(
        commit_sha, MODELZOO_WATCH_BRANCH,
        (payload.get("pusher") or {}).get("name", "unknown"),
        raw_body.decode(),
    )


@app.get("/models/{name}/meta")
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


@app.get("/models/{name}/readme")
def get_model_readme(name: str) -> dict[str, str]:
    if _model_meta_mod is None:
        raise HTTPException(503, "Model metadata service not available")
    text, sha = _model_meta_mod.read_readme(name)
    return {"text": text, "sha": sha}


@app.get("/models/{name}/images/{filename}")
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
    dependencies=[Depends(_require_token), Depends(_check_rate_limit)],
)
def notify_changes(notification: ChangeNotification) -> dict[str, Any]:
    created: list[str] = []
    created_model_ids: list[str] = []
    now = datetime.utcnow().isoformat()
    changed_files_json = json.dumps(notification.changed_files)

    with _DB_LOCK:
        conn = _get_db()
        try:
            for model_id in notification.model_ids:
                existing = conn.execute(
                    "SELECT id FROM pending_approvals WHERE model_id = ? AND status = 'pending'",
                    (model_id,),
                ).fetchone()
                if existing:
                    logger.info("Skipping duplicate pending approval model=%s (id=%s)", model_id, existing[0])
                    continue
                row_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO pending_approvals (id, model_id, commit_sha, commit_msg, changed_files, status, requested_at) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (row_id, model_id, notification.commit_sha, notification.commit_msg, changed_files_json, now),
                )
                created.append(row_id)
                created_model_ids.append(model_id)
                logger.info("Created pending approval id=%s model=%s commit=%s", row_id, model_id, notification.commit_sha)
            conn.commit()
            pending_count: int = conn.execute(
                "SELECT COUNT(*) FROM pending_approvals WHERE status = 'pending'"
            ).fetchone()[0]
        finally:
            conn.close()

    for mid in created_model_ids:
        _metrics.record_created(mid, pending_count)
    return {"created": created}


@app.get("/approvals", response_model=list[ApprovalEntry])
def list_approvals(status: str | None = None) -> list[ApprovalEntry]:
    conn = _get_db()
    try:
        sql = (
            "SELECT id, model_id, commit_sha, commit_msg, changed_files, "
            "status, prefect_run_id, reject_reason, requested_at, resolved_at "
            "FROM pending_approvals"
        )
        rows = (
            conn.execute(sql + " WHERE status = ? ORDER BY requested_at DESC", (status,)).fetchall()
            if status else
            conn.execute(sql + " ORDER BY requested_at DESC").fetchall()
        )
    finally:
        conn.close()

    entries: list[ApprovalEntry] = []
    for row in rows:
        row_id, model_id, commit_sha, commit_msg, changed_files_raw, row_status, prefect_run_id, reject_reason, requested_at, resolved_at = row
        try:
            changed_files = json.loads(changed_files_raw) if changed_files_raw else []
        except (TypeError, ValueError):
            changed_files = []
        entries.append(ApprovalEntry(
            id=row_id, model_id=model_id, commit_sha=commit_sha, commit_msg=commit_msg,
            changed_files=changed_files, status=row_status, prefect_run_id=prefect_run_id,
            reject_reason=reject_reason, requested_at=requested_at, resolved_at=resolved_at,
        ))
    return entries


@app.post(
    "/approve/{model_id}",
    dependencies=[Depends(_require_token), Depends(_check_rate_limit)],
)
def approve_model(model_id: str) -> dict[str, Any]:
    # Improvement 16: reject expired entries before approving
    _expire_old_approvals()

    with _DB_LOCK:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT id FROM pending_approvals WHERE model_id = ? AND status = 'pending' "
                "ORDER BY requested_at DESC LIMIT 1",
                (model_id,),
            ).fetchone()
        finally:
            conn.close()

    if not row:
        raise HTTPException(404, f"No pending approval found for model {model_id!r}")
    row_id: str = row[0]

    registry = _get_registry()
    datasets = registry.get(model_id, [])
    if not datasets:
        raise HTTPException(400, f"Model {model_id!r} has no registered datasets")
    dataset_name = datasets[0]

    parameters: dict[str, Any] = {
        "model_name": model_id, "dataset_cls_name": dataset_name,
        "is_dummy": False, "backend_name": None,
    }

    gateway = _get_gateway()
    deployment_id = gateway.find_deployment_id(PREFECT_DEPLOYMENT_NAME)
    flow_run_id: str = gateway.create_flow_run(deployment_id, parameters)

    pending_count = 0
    with _DB_LOCK:
        conn = _get_db()
        try:
            conn.execute(
                "UPDATE pending_approvals SET status = 'approved', prefect_run_id = ?, resolved_at = ? WHERE id = ?",
                (flow_run_id, datetime.utcnow().isoformat(), row_id),
            )
            conn.commit()
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM pending_approvals WHERE status = 'pending'"
            ).fetchone()[0]
        finally:
            conn.close()

    _metrics.record_approved(model_id, pending_count)
    logger.info("Approved model=%s approval_id=%s flow_run_id=%s", model_id, row_id, flow_run_id)
    return {"flow_run_id": flow_run_id, "status_url": f"/retrain/{flow_run_id}", "model_id": model_id}


@app.post(
    "/reject/{model_id}",
    dependencies=[Depends(_require_token), Depends(_check_rate_limit)],
)
def reject_model(model_id: str, body: RejectRequest = RejectRequest()) -> dict[str, Any]:
    pending_count = 0
    with _DB_LOCK:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT id FROM pending_approvals WHERE model_id = ? AND status = 'pending' "
                "ORDER BY requested_at DESC LIMIT 1",
                (model_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, f"No pending approval found for model {model_id!r}")
            row_id = row[0]
            conn.execute(
                "UPDATE pending_approvals SET status = 'rejected', reject_reason = ?, resolved_at = ? WHERE id = ?",
                (body.reason, datetime.utcnow().isoformat(), row_id),
            )
            conn.commit()
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM pending_approvals WHERE status = 'pending'"
            ).fetchone()[0]
        finally:
            conn.close()

    _metrics.record_rejected(model_id, pending_count)
    logger.info("Rejected model=%s approval_id=%s reason=%s", model_id, row_id, body.reason)
    return {"model_id": model_id, "status": "rejected"}


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint() -> Response:
    try:
        with _DB_LOCK:
            conn = _get_db()
            try:
                row = conn.execute(
                    "SELECT MIN(requested_at) FROM pending_approvals WHERE status = 'pending'"
                ).fetchone()
            finally:
                conn.close()
        _metrics.update_age(row[0])
    except Exception as exc:
        logger.error("metrics_endpoint DB read failed: %s", exc)
        _metrics.update_age(None)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/modelzoo/status")
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
            models.append({
                "model_id": model_id, "status": "stale" if row[3] else "current",
                "latest_modelzoo_commit": row[1], "last_retrain_commit": row[2],
                "stale_since": row[4], "retrain_triggered_at": row[5],
            })
        else:
            models.append({
                "model_id": model_id, "status": "unknown",
                "latest_modelzoo_commit": None, "last_retrain_commit": None,
                "stale_since": None, "retrain_triggered_at": None,
            })

    return {
        "models": models,
        "last_event": {"commit_sha": last_event[0], "timestamp": last_event[1], "source": last_event[2]}
        if last_event else None,
    }


@app.get("/modelzoo/events")
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
    return [{"id": r[0], "commit_sha": r[1], "branch": r[2], "pushed_by": r[3], "timestamp": r[4], "source": r[5]}
            for r in rows]


@app.post("/modelzoo/sync", dependencies=[Depends(_require_token)])
def modelzoo_sync() -> dict[str, Any]:
    result = _run_poll_cycle()
    if not result:
        return {"new_commit": False}
    return {"new_commit": True, "commit_sha": result.get("commit_sha"), "models_marked_stale": len(_get_registry())}


@app.get("/modelzoo/config")
def get_modelzoo_config() -> dict[str, Any]:
    with _CONFIG_LOCK:
        return dict(_modelzoo_config)


class ModelzooConfigUpdate(BaseModel):
    auto_retrain: bool | None = None
    poll_interval_seconds: int | None = None


@app.put("/modelzoo/config", dependencies=[Depends(_require_token)])
def update_modelzoo_config(body: ModelzooConfigUpdate) -> dict[str, Any]:
    with _CONFIG_LOCK:
        if body.auto_retrain is not None:
            _modelzoo_config["auto_retrain"] = body.auto_retrain
        if body.poll_interval_seconds is not None:
            _modelzoo_config["poll_interval_seconds"] = max(0, body.poll_interval_seconds)
        return dict(_modelzoo_config)


# ─── Improvement 19: Config hot-reload ───────────────────────────────────────


@app.post("/admin/reload", dependencies=[Depends(_require_token)])
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
