"""
ExaMLOps Control Plane — retraining trigger API.

Phase 4: a thin FastAPI service in front of Prefect that lets clients (and
the synthetic dataplane simulator) request a retraining run for a registered
model without holding Prefect API credentials themselves.

Phase 11: approval gate — GitLab CI (or any webhook) can POST /api/changes to
notify of model changes; a human then approves or rejects via POST /approve/{model_id}
or POST /reject/{model_id}. Approved changes trigger a Prefect flow run exactly
like POST /retrain. State is stored in a local SQLite database.

Endpoints (served on CONTROL_PLANE_PORT, default 8002):
    GET  /health                   liveness + reachable Prefect URL + pending count
    GET  /status                   aggregate health of all ExaMLOps services
    GET  /models                   list of model_name → datasets known to the registry
    POST /retrain                  body: {model_name, dataset_name, [backend_name], [is_dummy], [parameters]}
    GET  /retrain/{flow_run_id}    poll Prefect for the run state
    POST /api/changes              notify of model changes (requires token); creates pending approvals
    GET  /approvals                list approvals, optional ?status=pending filter
    POST /approve/{model_id}       approve the most recent pending change (requires token)
    POST /reject/{model_id}        reject the most recent pending change (requires token)

Auth:
    POST /retrain, POST /api/changes, POST /approve/*, POST /reject/* require
    `Authorization: Bearer <CONTROL_PLANE_TOKEN>`.
    The token is shared with the dataplane / clients via env. When the env
    var is unset the service refuses to start — accidental "no auth" mode
    is the wrong default for an EU-project deployment.

Env vars:
    CONTROL_PLANE_PORT         default: 8002
    CONTROL_PLANE_TOKEN        REQUIRED — bearer token for POST /retrain
    PREFECT_API_URL            default: http://localhost:4200/api
    PREFECT_DEPLOYMENT_NAME    default: examlops_scheduled_training/nightly
                               (deployment that wraps training_flow)
    CONTROL_PLANE_DB           default: /data/approvals.db — SQLite file for approval state
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any

import metrics as _metrics
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [control-plane] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
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

# Runtime-mutable ModelZoo config (survives process lifetime, resets on restart)
_modelzoo_config: dict[str, Any] = {
    "auto_retrain": MODELZOO_AUTO_RETRAIN,
    "poll_interval_seconds": MODELZOO_POLL_SECONDS,
    "watch_branch": MODELZOO_WATCH_BRANCH,
}


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


def _get_db() -> sqlite3.Connection:
    """Open (or create) the approvals SQLite DB and ensure the table exists."""
    db_path = CONTROL_PLANE_DB
    # Ensure parent directory exists when running locally with a path like ./approvals.db
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except OSError:
            # Fall back to a local file if /data/ is not writable (e.g. unit tests)
            db_path = "./approvals.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(_CREATE_MODELZOO_EVENTS_SQL)
    conn.execute(_CREATE_MODEL_FRESHNESS_SQL)
    conn.commit()
    return conn


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
    backend_name: str | None = Field(
        default=None,
        description="Phase 1 storage backend ('zenodo' / 'minio' / 'dataplane'). None = legacy.",
    )
    is_dummy: bool = Field(
        default=False,
        description="Run with dummy data — recommended for drift-trigger retrains until validated.",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra Prefect flow parameters merged into the run request.",
    )


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


# ─── App + auth ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="ExaMLOps Control Plane",
    version="0.11.0",
    description="Authoritative entry point for client-driven retraining requests and approval gates.",
)


@app.on_event("startup")
def _on_startup() -> None:
    _start_poller()


def _require_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token auth dependency for write endpoints."""
    if not CONTROL_PLANE_TOKEN:
        # Fail closed — never silently authenticate when no token was set.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Control plane not configured (CONTROL_PLANE_TOKEN env var unset).",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    if authorization.removeprefix("Bearer ").strip() != CONTROL_PLANE_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid bearer token")


# ─── ModelZoo webhook auth helpers ───────────────────────────────────────────


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


def _record_push_event(
    commit_sha: str, branch: str, pushed_by: str, raw_payload: str
) -> dict[str, Any]:
    """Insert event row, update all model_freshness rows to stale. Returns result dict."""
    now = datetime.utcnow().isoformat()
    registry = _load_registry()
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
                    """
                    INSERT INTO model_freshness (model_id, latest_modelzoo_commit, is_stale, stale_since)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(model_id) DO UPDATE SET
                        latest_modelzoo_commit = excluded.latest_modelzoo_commit,
                        is_stale = 1,
                        stale_since = excluded.stale_since
                    """,
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

    return {
        "event_id": event_id,
        "models_marked_stale": len(registry),
        "retrain_triggered": retrain_triggered,
    }


def _auto_retrain_model(model_id: str, dataset_name: str, commit_sha: str) -> None:
    """Trigger Prefect retrain and record retrain_triggered_at."""
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
                """
                INSERT INTO model_freshness (model_id, latest_modelzoo_commit, last_retrain_commit,
                    is_stale, retrain_triggered_at)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(model_id) DO UPDATE SET
                    last_retrain_commit = excluded.latest_modelzoo_commit,
                    is_stale = 0,
                    retrain_triggered_at = excluded.retrain_triggered_at
                """,
                (model_id, commit_sha, commit_sha, now),
            )
            conn.commit()
        finally:
            conn.close()
    logger.info("Auto-retrain triggered model=%s flow_run_id=%s", model_id, flow_run_id)


def _run_poll_cycle() -> dict[str, Any]:
    """Check GitLab for the latest commit on MODELZOO_WATCH_BRANCH.

    Returns {"new": True, "commit_sha": sha} if a new commit was found,
    or {} if already up-to-date or credentials are missing.
    """
    if not GITLAB_TOKEN or not GITLAB_PROJECT_ID:
        logger.debug("ModelZoo poller: GITLAB_TOKEN or GITLAB_PROJECT_ID not set, skipping")
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

    # Record the event — check for duplicate SHA inside the lock to avoid TOCTOU race
    now = datetime.utcnow().isoformat()
    registry = _load_registry()
    event_id: int = 0
    with _DB_LOCK:
        conn = _get_db()
        try:
            # Re-check inside lock to prevent concurrent webhook and poll from inserting duplicates
            existing = conn.execute(
                "SELECT id FROM modelzoo_events WHERE commit_sha = ?", (latest_sha,)
            ).fetchone()
            if existing:
                return {}
            conn.execute(
                "INSERT INTO modelzoo_events (commit_sha, branch, pushed_by, timestamp, source) "
                "VALUES (?, ?, ?, ?, 'poll')",
                (latest_sha, MODELZOO_WATCH_BRANCH, pushed_by, committed_at),
            )
            event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for model_id in registry:
                conn.execute(
                    """
                    INSERT INTO model_freshness (model_id, latest_modelzoo_commit, is_stale, stale_since)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(model_id) DO UPDATE SET
                        latest_modelzoo_commit = excluded.latest_modelzoo_commit,
                        is_stale = 1,
                        stale_since = excluded.stale_since
                    """,
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
    """Start the background ModelZoo polling thread. No-op if poll interval is 0."""
    if MODELZOO_POLL_SECONDS <= 0:
        logger.info("ModelZoo poller disabled (MODELZOO_POLL_SECONDS=0)")
        return

    import time  # noqa: PLC0415

    def _loop() -> None:
        logger.info("ModelZoo poller started (interval=%ds)", MODELZOO_POLL_SECONDS)
        while True:
            time.sleep(_modelzoo_config.get("poll_interval_seconds", MODELZOO_POLL_SECONDS))
            try:
                _run_poll_cycle()
            except Exception as exc:
                logger.warning("ModelZoo poll cycle error: %s", exc)

    t = threading.Thread(target=_loop, daemon=True, name="modelzoo-poller")
    t.start()


# ─── Registry helpers ────────────────────────────────────────────────────────


def _load_registry() -> dict[str, list[str]]:
    """Return ``{model_name: [dataset_class_name, ...]}`` from YAML model files.

    Reads pipelines/models/*.yaml directly — avoids importing pipeline_generator
    which transitively requires torch via the modelzoo configurator.
    """
    import yaml  # pyyaml is a declared dep of examlops-pipelines

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
                datasets = [d["name"] for d in cfg.get("datasets", []) if d.get("name")]
                result[name] = datasets
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping %s: %s", fname, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("YAML registry scan failed: %s", exc)
    return result


# ─── Prefect client wrapper ──────────────────────────────────────────────────


class PrefectGateway:
    """Tiny urllib-based Prefect REST client.

    Avoids hard-pinning the prefect-client SDK in the control-plane image —
    we only need three endpoints (find deployment, create flow run, read flow
    run). Plain HTTP is plenty.
    """

    def __init__(self, api_url: str = PREFECT_API_URL) -> None:
        self.api_url = api_url.rstrip("/")

    def find_deployment_id(self, deployment_name: str) -> str:
        """Resolve a ``flow_name/deployment_name`` slug to a deployment UUID."""
        import urllib.parse  # noqa: PLC0415

        if "/" not in deployment_name:
            raise HTTPException(
                status_code=400,
                detail=f"deployment must be 'flow_name/deployment_name', got {deployment_name!r}",
            )
        flow_name, dep_name = deployment_name.split("/", 1)
        url = (
            f"{self.api_url}/deployments/name/"
            f"{urllib.parse.quote(flow_name)}/{urllib.parse.quote(dep_name)}"
        )
        payload = self._get(url)
        dep_id = payload.get("id")
        if not dep_id:
            raise HTTPException(
                status_code=502, detail=f"Prefect did not return an id for {deployment_name!r}"
            )
        return dep_id

    def create_flow_run(self, deployment_id: str, parameters: dict[str, Any]) -> str:
        """Schedule an immediate run for *deployment_id* with *parameters*."""
        url = f"{self.api_url}/deployments/{deployment_id}/create_flow_run"
        payload = self._post(url, body={"parameters": parameters})
        run_id = payload.get("id")
        if not run_id:
            raise HTTPException(
                status_code=502, detail="Prefect create_flow_run returned no id"
            )
        return run_id

    def get_flow_run(self, flow_run_id: str) -> dict[str, Any]:
        return self._get(f"{self.api_url}/flow_runs/{flow_run_id}")

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get(self, url: str) -> dict[str, Any]:
        import json  # noqa: PLC0415
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise HTTPException(
                status_code=exc.code, detail=f"Prefect GET {url} -> HTTP {exc.code}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Prefect unreachable: {exc}") from exc

    def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        import json  # noqa: PLC0415
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        data = json.dumps(body).encode("utf-8")
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
            raise HTTPException(
                status_code=exc.code, detail=f"Prefect POST {url} -> HTTP {exc.code}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Prefect unreachable: {exc}") from exc


_gateway: PrefectGateway | None = None


def _get_gateway() -> PrefectGateway:
    global _gateway
    if _gateway is None:
        _gateway = PrefectGateway()
    return _gateway


# ─── Endpoints ───────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict[str, Any]:
    pending_count = 0
    conn = None
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE status = 'pending'"
        ).fetchone()
        if row:
            pending_count = row[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not query pending approvals count: %s", exc)
    finally:
        if conn:
            conn.close()

    return {
        "status": "ok",
        "prefect_api_url": PREFECT_API_URL,
        "deployment": PREFECT_DEPLOYMENT_NAME,
        "auth_configured": bool(CONTROL_PLANE_TOKEN),
        "models": _load_registry(),
        "pending_approvals": pending_count,
    }


@app.get("/status")
def platform_status() -> dict[str, Any]:
    """Aggregate health snapshot of all ExaMLOps services."""
    import urllib.error  # noqa: PLC0415
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
        row = conn.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE status = 'pending'"
        ).fetchone()
        pending_count = row[0] if row else 0
    except Exception:  # noqa: BLE001
        pass
    finally:
        if conn:
            conn.close()

    ray_ok, ray_data = _ping_json(f"{RAY_SERVE_URL}/models")
    ray_models: list[str] = (
        [m.get("name", m) if isinstance(m, dict) else m for m in (ray_data or [])]
        if ray_ok else []
    )

    return {
        "services": {
            "control_plane": {"ok": True},
            "mlflow":        {"ok": _ping(f"{MLFLOW_URL}/health")},
            "prefect":       {"ok": _ping(f"{PREFECT_API_URL}/health")},
            "ray_serve":     {"ok": ray_ok, "models": ray_models},
            "dashboard":     {"ok": _ping(f"{DASHBOARD_URL}/api/health")},
        },
        "pending_approvals": pending_count,
    }


@app.get("/models", response_model=list[ModelEntry])
def list_models() -> list[ModelEntry]:
    """Models the control plane can trigger retrains for."""
    return [
        ModelEntry(model_name=name, datasets=datasets)
        for name, datasets in _load_registry().items()
    ]


@app.post("/retrain", response_model=RetrainResponse, dependencies=[Depends(_require_token)])
def trigger_retrain(req: RetrainRequest) -> RetrainResponse:
    """Schedule a Prefect flow run for ``training_flow`` with the given args.

    Validates that ``model_name`` and ``dataset_name`` are known to the
    auto-discovery registry before talking to Prefect, so a stale client can't
    spawn ghost runs.
    """
    registry = _load_registry()
    if req.model_name not in registry:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown model {req.model_name!r}. "
                f"Known models: {sorted(registry)}"
            ),
        )
    if req.dataset_name not in registry[req.model_name]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Dataset {req.dataset_name!r} not declared as supported by {req.model_name}. "
                f"Supported: {registry[req.model_name]}"
            ),
        )

    parameters: dict[str, Any] = {
        "model_name": req.model_name,
        "dataset_cls_name": req.dataset_name,
        "is_dummy": req.is_dummy,
        "backend_name": req.backend_name,
        **req.parameters,
    }

    gateway = _get_gateway()
    deployment_id = gateway.find_deployment_id(PREFECT_DEPLOYMENT_NAME)
    flow_run_id = gateway.create_flow_run(deployment_id, parameters)

    logger.info(
        "Scheduled retrain — model=%s dataset=%s flow_run_id=%s",
        req.model_name, req.dataset_name, flow_run_id,
    )
    return RetrainResponse(
        flow_run_id=flow_run_id,
        deployment=PREFECT_DEPLOYMENT_NAME,
        status_url=f"/retrain/{flow_run_id}",
        parameters=parameters,
    )


@app.get("/retrain/{flow_run_id}", response_model=FlowRunStatus)
def retrain_status(flow_run_id: str) -> FlowRunStatus:
    """Poll Prefect for the run state. Read-only — no auth required."""
    payload = _get_gateway().get_flow_run(flow_run_id)
    state = payload.get("state") or {}
    state_type = state.get("type")
    return FlowRunStatus(
        flow_run_id=flow_run_id,
        state_type=state_type,
        state_name=state.get("name"),
        is_terminal=state_type in ("COMPLETED", "FAILED", "CANCELLED", "CRASHED"),
    )


# ─── Phase 11 — ModelZoo webhook receivers ───────────────────────────────────


@app.post("/webhooks/modelzoo/gitlab")
async def webhook_gitlab(
    request: Request,
    x_gitlab_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Receive a GitLab push webhook for the ModelZoo repository."""
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
    """Receive a GitHub push webhook for the ModelZoo repository."""
    raw_body = await request.body()
    sig = request.headers.get("x-hub-signature-256")
    _verify_github_signature(raw_body, sig)
    payload = json.loads(raw_body)
    ref = payload.get("ref", "")
    if not ref.endswith(f"/{MODELZOO_WATCH_BRANCH}"):
        return {"skipped": True, "reason": f"branch {ref!r} is not {MODELZOO_WATCH_BRANCH!r}"}
    head = payload.get("head_commit") or {}
    commit_sha = head.get("id", payload.get("after", ""))
    if not commit_sha:
        return {"skipped": True, "reason": "no commit SHA in payload"}
    pushed_by = (payload.get("pusher") or {}).get("name", "unknown")
    return _record_push_event(commit_sha, MODELZOO_WATCH_BRANCH, pushed_by, raw_body.decode())


@app.get("/models/{name}/meta")
def get_model_meta_endpoint(name: str) -> dict[str, Any]:
    """Fetch metadata for a registered model (schema, task type, promotion logic)."""
    if _model_meta_mod is None:
        raise HTTPException(
            status_code=503,
            detail="Model metadata service not available",
        )
    try:
        m = _model_meta_mod.get_model_meta(name)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown model {name!r}") from exc
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
    }


@app.get("/models/{name}/readme")
def get_model_readme(name: str) -> dict[str, str]:
    """Fetch the README.md content for a model."""
    if _model_meta_mod is None:
        raise HTTPException(
            status_code=503,
            detail="Model metadata service not available",
        )
    text, sha = _model_meta_mod.read_readme(name)
    return {"text": text, "sha": sha}


@app.get("/models/{name}/images/{filename}")
def get_model_bundled_image(name: str, filename: str) -> Response:
    """Fetch a bundled image (PNG, JPG, SVG, etc.) for a model."""
    if _model_meta_mod is None:
        raise HTTPException(
            status_code=503,
            detail="Model metadata service not available",
        )
    found = _model_meta_mod.read_image(name, filename)
    if found is None:
        raise HTTPException(status_code=404, detail="Image not found")
    data, content_type = found
    return Response(content=data, media_type=content_type)


# ─── Phase 11 — approval gate endpoints ─────────────────────────────────────


@app.post("/api/changes", dependencies=[Depends(_require_token)])
def notify_changes(notification: ChangeNotification) -> dict[str, Any]:
    """Receive a change notification (e.g. from GitLab CI) and create pending approval rows.

    One row is created per model_id. If a pending row for that model_id already
    exists it is skipped — no duplicate pending entries.
    """
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
                    logger.info(
                        "Skipping duplicate pending approval for model=%s (existing id=%s)",
                        model_id,
                        existing[0],
                    )
                    continue
                row_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO pending_approvals
                        (id, model_id, commit_sha, commit_msg, changed_files,
                         status, requested_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        row_id,
                        model_id,
                        notification.commit_sha,
                        notification.commit_msg,
                        changed_files_json,
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
                "SELECT COUNT(*) FROM pending_approvals WHERE status = 'pending'"
            ).fetchone()[0]
        finally:
            conn.close()

    for mid in created_model_ids:
        _metrics.record_created(mid, pending_count)

    return {"created": created}


@app.get("/approvals", response_model=list[ApprovalEntry])
def list_approvals(status: str | None = None) -> list[ApprovalEntry]:
    """List approval entries. Pass ``?status=pending`` to filter by status."""
    conn = _get_db()
    try:
        if status:
            rows = conn.execute(
                "SELECT id, model_id, commit_sha, commit_msg, changed_files, "
                "status, prefect_run_id, reject_reason, requested_at, resolved_at "
                "FROM pending_approvals WHERE status = ? ORDER BY requested_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, model_id, commit_sha, commit_msg, changed_files, "
                "status, prefect_run_id, reject_reason, requested_at, resolved_at "
                "FROM pending_approvals ORDER BY requested_at DESC"
            ).fetchall()
    finally:
        conn.close()

    entries: list[ApprovalEntry] = []
    for row in rows:
        (
            row_id, model_id, commit_sha, commit_msg, changed_files_raw,
            row_status, prefect_run_id, reject_reason, requested_at, resolved_at,
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
                requested_at=requested_at,
                resolved_at=resolved_at,
            )
        )
    return entries


@app.post("/approve/{model_id}", dependencies=[Depends(_require_token)])
def approve_model(model_id: str) -> dict[str, Any]:
    """Approve the most recent pending change for *model_id* and trigger a Prefect flow run."""
    # Step 1: read pending row — short DB op, lock released immediately after
    with _DB_LOCK:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT id FROM pending_approvals "
                "WHERE model_id = ? AND status = 'pending' "
                "ORDER BY requested_at DESC LIMIT 1",
                (model_id,),
            ).fetchone()
        finally:
            conn.close()

    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"No pending approval found for model {model_id!r}",
        )
    row_id: str = row[0]

    # Determine dataset: use first supported dataset from registry
    registry = _load_registry()
    datasets = registry.get(model_id, [])
    if not datasets:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model {model_id!r} has no registered datasets — "
                "cannot determine which dataset to retrain on."
            ),
        )
    dataset_name = datasets[0]

    parameters: dict[str, Any] = {
        "model_name": model_id,
        "dataset_cls_name": dataset_name,
        "is_dummy": False,
        "backend_name": None,
    }

    # Step 2: call Prefect WITHOUT holding the lock (blocking network I/O, up to 10 s)
    gateway = _get_gateway()
    deployment_id = gateway.find_deployment_id(PREFECT_DEPLOYMENT_NAME)
    flow_run_id: str = gateway.create_flow_run(deployment_id, parameters)

    # Step 3: update DB row — short DB op, lock released immediately after
    pending_count = 0
    with _DB_LOCK:
        conn = _get_db()
        try:
            now = datetime.utcnow().isoformat()
            conn.execute(
                "UPDATE pending_approvals "
                "SET status = 'approved', prefect_run_id = ?, resolved_at = ? "
                "WHERE id = ?",
                (flow_run_id, now, row_id),
            )
            conn.commit()
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM pending_approvals WHERE status = 'pending'"
            ).fetchone()[0]
        finally:
            conn.close()
    _metrics.record_approved(model_id, pending_count)

    logger.info(
        "Approved model=%s approval_id=%s flow_run_id=%s", model_id, row_id, flow_run_id
    )
    return {
        "flow_run_id": flow_run_id,
        "status_url": f"/retrain/{flow_run_id}",
        "model_id": model_id,
    }


@app.post("/reject/{model_id}", dependencies=[Depends(_require_token)])
def reject_model(model_id: str, body: RejectRequest = RejectRequest()) -> dict[str, Any]:
    """Reject the most recent pending change for *model_id*."""
    pending_count = 0
    with _DB_LOCK:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT id FROM pending_approvals "
                "WHERE model_id = ? AND status = 'pending' "
                "ORDER BY requested_at DESC LIMIT 1",
                (model_id,),
            ).fetchone()
            # Lock is released even when HTTPException propagates (Python `with` guarantee).
            if not row:
                raise HTTPException(
                    status_code=404,
                    detail=f"No pending approval found for model {model_id!r}",
                )
            row_id = row[0]
            now = datetime.utcnow().isoformat()
            conn.execute(
                "UPDATE pending_approvals "
                "SET status = 'rejected', reject_reason = ?, resolved_at = ? "
                "WHERE id = ?",
                (body.reason, now, row_id),
            )
            conn.commit()
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM pending_approvals WHERE status = 'pending'"
            ).fetchone()[0]
        finally:
            conn.close()

    _metrics.record_rejected(model_id, pending_count)
    logger.info(
        "Rejected model=%s approval_id=%s reason=%s", model_id, row_id, body.reason
    )
    return {"model_id": model_id, "status": "rejected"}


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint() -> Response:
    """Prometheus text-format metrics — scraped by Prometheus, no auth required."""
    try:
        with _DB_LOCK:
            conn = _get_db()
            try:
                row = conn.execute(
                    "SELECT MIN(requested_at) FROM pending_approvals WHERE status = 'pending'"
                ).fetchone()
            finally:
                conn.close()
        # MIN() always returns one row; row[0] is None when the table is empty.
        _metrics.update_age(row[0])
    except Exception as exc:
        logger.error("metrics_endpoint: DB read failed, age gauge set to 0: %s", exc)
        _metrics.update_age(None)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ─── Phase 12 — ModelZoo integration endpoints ───────────────────────────────


@app.get("/modelzoo/status")
def modelzoo_status() -> dict[str, Any]:
    """Return the freshness status of all registered models relative to the ModelZoo."""
    conn = _get_db()
    try:
        freshness_rows = conn.execute(
            "SELECT model_id, latest_modelzoo_commit, last_retrain_commit, "
            "is_stale, stale_since, retrain_triggered_at FROM model_freshness"
        ).fetchall()
        last_event = conn.execute(
            "SELECT commit_sha, timestamp, source FROM modelzoo_events "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    freshness_map = {row[0]: row for row in freshness_rows}
    registry = _load_registry()
    models = []
    for model_id in registry:
        row = freshness_map.get(model_id)
        if row:
            models.append({
                "model_id": model_id,
                "status": "stale" if row[3] else "current",
                "latest_modelzoo_commit": row[1],
                "last_retrain_commit": row[2],
                "stale_since": row[4],
                "retrain_triggered_at": row[5],
            })
        else:
            models.append({
                "model_id": model_id,
                "status": "unknown",
                "latest_modelzoo_commit": None,
                "last_retrain_commit": None,
                "stale_since": None,
                "retrain_triggered_at": None,
            })

    last_event_dict = None
    if last_event:
        last_event_dict = {
            "commit_sha": last_event[0],
            "timestamp": last_event[1],
            "source": last_event[2],
        }
    return {"models": models, "last_event": last_event_dict}


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
    return [
        {"id": r[0], "commit_sha": r[1], "branch": r[2],
         "pushed_by": r[3], "timestamp": r[4], "source": r[5]}
        for r in rows
    ]


@app.post("/modelzoo/sync", dependencies=[Depends(_require_token)])
def modelzoo_sync() -> dict[str, Any]:
    """Manually trigger one poll cycle. Returns result of the check."""
    result = _run_poll_cycle()
    if not result:
        return {"new_commit": False}
    return {
        "new_commit": True,
        "commit_sha": result.get("commit_sha"),
        "models_marked_stale": len(_load_registry()),
    }


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


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    if not CONTROL_PLANE_TOKEN:
        logger.warning(
            "CONTROL_PLANE_TOKEN is empty — POST /retrain will fail with 503 "
            "until you set it. Read-only endpoints will still work."
        )
    uvicorn.run(app, host="0.0.0.0", port=CONTROL_PLANE_PORT, log_level="info")


if __name__ == "__main__":
    main()
