"""
Ray Serve Multi-Model Inference Service – ExaMLOps.

Phase 3: serves multiple MLflow lifecycle aliases (Staging / Canary /
Production / Archived) and supports request-time version selection. A new
training run that promotes a Production version is reflected in serving
within seconds via two complementary paths:

* **Webhook** — ``promote_task`` POSTs to ``/reload/{model_name}`` immediately
  after setting the alias.
* **Polling** — a background task polls MLflow every
  ``RAY_RELOAD_POLL_SECONDS`` and reloads any model whose alias version moved.
  This is the safety net when the webhook is unreachable.

Routes (served on RAY_SERVE_PORT, default 8001):
    GET  /health                   liveness + per-model status
    GET  /models                   list all loaded (alias, version) entries
    POST /predict/{model_name}     inference with optional alias / version selection
    POST /reload                   re-scan MLflow and hot-reload all hot-alias models
    POST /reload/{model_name}      hot-reload a single model
    GET  /docs                     FastAPI Swagger UI
    GET  /redoc                    FastAPI ReDoc UI

Env vars:
    MLFLOW_TRACKING_URI         default: http://localhost:15000
    MODEL_STAGE                 default: Production            (default alias)
    RAY_PRELOAD_ALIASES         default: "Production,Canary,Staging"
    RAY_VERSION_CACHE_SIZE      default: 8                     (LRU size for raw versions)
    RAY_RELOAD_POLL_SECONDS     default: 60                    (0 disables MLflow polling)
    RAY_SNAPSHOT_MODE           default: auto                  (serve from the control plane's
                                                                serving snapshot; off = legacy)
    RAY_SNAPSHOT_POLL_SECONDS   default: 2                     (how often a newer generation is
                                                                looked for)
    RAY_INPUT_SCHEMA            default: enforce               (check requests against the input
                                                                schema the snapshot carries; off
                                                                = the model's own signature only)
    RAY_SNAPSHOT_CACHE          default: <tmp>/examlops-serving-snapshot.json (last-known-good)
    RAY_ARTIFACT_CACHE          default: off                   (directory of content-addressed
                                                                model versions)
    RAY_ARTIFACT_CACHE_MAX_GB   default: 20                    (LRU bound for that directory)
    RAY_NUM_REPLICAS            default: 2                     (the floor, with autoscaling on)
    RAY_AUTOSCALE_MAX_REPLICAS  default: unset                 (set: Ray autoscales up to it)
    RAY_AUTOSCALE_TARGET_ONGOING      default: 5               (in-flight requests per replica)
    RAY_AUTOSCALE_UPSCALE_DELAY_S     default: 30
    RAY_AUTOSCALE_DOWNSCALE_DELAY_S   default: 300
    RAY_MAX_QUEUED_REQUESTS     default: -1                    (unbounded; set it to shed load)
    RAY_GAUGE_REFRESH_SECONDS   default: 5                     (republish gauges; Ray 2.55 drops
                                                                a gauge not set since its last export)
    RAY_SERVE_PORT              default: 8001
    RAY_SERVE_GRPC_PORT         default: 8081                  (Open Inference Protocol v2 over
                                                                gRPC; 0 turns it off)
    RAY_SERVE_GRPC_MAX_MESSAGE_MB default: 8                   (largest gRPC request or answer)
    RAY_SERVE_HOST              default: 0.0.0.0               (127.0.0.1 under the workload-
                                                                identity overlay: the mTLS sidecar
                                                                in this namespace is the way in)
"""

# NOTE: deliberately NOT using `from __future__ import annotations`.
# PEP 563 stringifies all annotations, and Ray Serve's `@serve.ingress` wrapper
# with recent FastAPI/Starlette/pydantic fails to resolve the postponed string
# annotations on endpoint body parameters — demoting a Pydantic-model body
# param to a query param (HTTP 422) or failing to build its TypeAdapter (500).
# Keeping annotations as real objects lets FastAPI introspect request bodies.

import os as _os
import sys as _sys
from pathlib import Path as _Path

try:
    _REPO_ROOT_RS = _Path(__file__).resolve().parents[2]
    for _p in (
        str(_REPO_ROOT_RS),
        str(_REPO_ROOT_RS / "pipelines"),
        _os.environ.get("EXAMLOPS_MODELZOO_DIR") or str(_REPO_ROOT_RS / "modelzoo"),
    ):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
except IndexError:
    pass

import asyncio
import logging
import os
import signal
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Annotated, Any

import mlflow
import mlflow.pyfunc
import ray
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from ray import serve
from ray.util.metrics import Counter, Gauge, Histogram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ray_serving")

# ─── Config ──────────────────────────────────────────────────────────────────

MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:15000")
MODEL_STAGE = os.getenv("MODEL_STAGE", "Production")
NUM_REPLICAS = int(os.getenv("RAY_NUM_REPLICAS", "2"))
SERVE_PORT = int(os.getenv("RAY_SERVE_PORT", "8001"))
# The address the HTTP proxy binds. Loopback leaves the model server reachable only from its own
# network namespace, where the mutual-TLS sidecar runs (ADR 0125 phase 3).
SERVE_HOST = os.getenv("RAY_SERVE_HOST", "0.0.0.0").strip() or "0.0.0.0"
# The Open Inference Protocol v2 over gRPC (ADR 0126), on the same host as REST. 8081 is the port
# KServe and MLServer use for it. It answers through the REST implementation on loopback.
GRPC_PORT = int(os.getenv("RAY_SERVE_GRPC_PORT", "8081") or 0)
GRPC_MAX_MESSAGE_BYTES = int(float(os.getenv("RAY_SERVE_GRPC_MAX_MESSAGE_MB", "8")) * 1024 * 1024)
METRICS_EXPORT_PORT = int(os.getenv("RAY_METRICS_EXPORT_PORT", "8080"))

PRELOAD_ALIASES = [
    a.strip()
    for a in os.getenv("RAY_PRELOAD_ALIASES", "Production,Canary,Staging").split(",")
    if a.strip()
]
VERSION_CACHE_SIZE = int(os.getenv("RAY_VERSION_CACHE_SIZE", "8"))
RELOAD_POLL_SECONDS = int(os.getenv("RAY_RELOAD_POLL_SECONDS", "60"))

# ── Fault tolerance ────────────────────────────────────────────────────────────
# Bound every MLflow REST call. MLflow reads these env vars natively (a requests-
# level connect/read timeout + retry with backoff), so a hung or unreachable
# tracking server can no longer block replica startup, the poller, or the request
# path indefinitely. setdefault ⇒ operator overrides win.
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", os.getenv("RAY_MLFLOW_TIMEOUT", "10"))
os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", os.getenv("RAY_MLFLOW_MAX_RETRIES", "3"))
# MLflow 3.x types this env var as int (default 2); a float like "0.5" raises
# ValueError in every REST call ("invalid literal for int()"), silently killing the
# alias-change scan so the hot set never fills. Keep it an integer.
os.environ.setdefault(
    "MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR", os.getenv("RAY_MLFLOW_BACKOFF_FACTOR", "1")
)

# Hard ceiling on a single model.predict() so a pathological/hung model can't pin
# a replica worker forever (num_cpus=1 ⇒ a hang otherwise saturates the deployment).
PREDICT_TIMEOUT = float(os.getenv("RAY_PREDICT_TIMEOUT", "30"))

# Retry the startup hot-set scan so a transient MLflow blip at boot doesn't leave
# the replica permanently empty until the next poll cycle.
from examlops.resilience import is_transient_network, retry_call  # noqa: E402
from serving.admin_auth import require_serving_admin  # noqa: E402
from serving.budgets import HEADER as BUDGET_HEADER  # noqa: E402
from serving.budgets import Deadline  # noqa: E402
from serving.ray_serving import oip  # noqa: E402
from serving.ray_serving.artifact_cache import ArtifactCache  # noqa: E402
from serving.ray_serving.snapshot import SnapshotReader  # noqa: E402

# Serving snapshot (ADR 0127, plan P4.2): `auto` serves from the snapshot the control plane
# publishes whenever one exists and falls back to scanning MLflow when none does; `off` keeps the
# legacy per-replica MLflow polling only.
SNAPSHOT_MODE = os.getenv("RAY_SNAPSHOT_MODE", "auto").strip().lower()
# Input schemas the snapshot carries (ADR 0123 d3), `RAY_INPUT_SCHEMA` (read per request, see
# `_snapshot_signature`): `enforce` (default) refuses a request that does not fit with a 422 naming
# the field; `off` ignores them. A model whose snapshot entry has no schema — or a malformed one —
# is served exactly as before either way (fail open).
SNAPSHOT_POLL_SECONDS = max(0.5, float(os.getenv("RAY_SNAPSHOT_POLL_SECONDS", "2")))
# Ray 2.55 exports a gauge only for the report interval in which it was set, so the replica
# publishes its gauges again on this cadence (see MultiModelServer._publish_gauges). 0 disables.
GAUGE_REFRESH_SECONDS = float(os.getenv("RAY_GAUGE_REFRESH_SECONDS", "5"))

# Verify-before-load (plan P0.6 / finding S3): `off` | `warn` (default since models are signed at
# registration, plan P4.10: every load is checked and a failure audited, nothing is refused) |
# `enforce`. Serving loads pickled artifacts, so whoever can move an MLflow alias or write the
# artifact store can otherwise run code in this process; `enforce` is what closes that.
_VERIFY_MODE = os.getenv("EXAMLOPS_SERVING_VERIFY", "warn").strip().lower()


# Content-addressed local copies of model versions (ADR 0127 decision 5, plan P4.3): with the
# snapshot, what lets a replica restart and serve while MLflow is down. Off unless configured.
_ARTIFACT_CACHE = ArtifactCache.from_env()


def _verified_uri(name: str, version: str, uri: str, *, record: dict | None = None) -> str:
    """The URI to load: ``uri`` itself, or a local copy whose bytes passed signature verification.

    With ``RAY_ARTIFACT_CACHE`` set, the copy comes from the artifact cache (fetched by version on
    a miss, digest-checked on a hit), and verification runs against those cached bytes.

    In ``warn``/``enforce`` mode the artifacts are downloaded once, verified with
    ``examlops.supplychain.verify_before_load``, and the *verified local copy* is what gets loaded.
    Loading ``uri`` again after checking a separate download would verify one set of bytes and run
    another. ``enforce`` refuses (raises) on an unsigned, tampered or unverifiable artifact; the
    hot-set loader then keeps serving the last-known-good version.

    ``record`` is the signature the serving snapshot carries for this version; with it nothing is
    read from the datastore. Without it the ``model_signatures`` row is looked up.
    """
    cache = _ARTIFACT_CACHE
    if cache is not None:
        try:
            cached = cache.fetch(name, version)
        except Exception as exc:  # noqa: BLE001 - the registry path below is the fallback
            logger.warning("Artifact cache could not supply %s v%s: %s", name, version, exc)
        else:
            if _VERIFY_MODE in ("warn", "enforce"):
                from examlops.supplychain import verify_before_load  # noqa: PLC0415
                from serving.ray_serving.artifact_cache import MANIFEST  # noqa: PLC0415

                paths = [p for p in cached.rglob("*") if p.is_file() and p.name != MANIFEST]
                if not verify_before_load(
                    name, version, paths, mode=_VERIFY_MODE, root=cached, record=record
                ):
                    cache.discard(name, version)  # never load, or keep, bytes that failed
                    raise RuntimeError(
                        f"refusing to load {name} v{version}: signature verification failed "
                        f"(EXAMLOPS_SERVING_VERIFY={_VERIFY_MODE}; sign with `exa models sign`)"
                    )
            return str(cached)
    if _VERIFY_MODE not in ("warn", "enforce"):
        return uri
    from pathlib import Path  # noqa: PLC0415

    from examlops.supplychain import verify_before_load  # noqa: PLC0415

    local = mlflow.artifacts.download_artifacts(artifact_uri=uri)
    paths = [p for p in Path(local).rglob("*") if p.is_file()]
    if not verify_before_load(
        name, version, paths, mode=_VERIFY_MODE, root=Path(local), record=record
    ):
        raise RuntimeError(
            f"refusing to load {name} v{version}: signature verification failed "
            f"(EXAMLOPS_SERVING_VERIFY={_VERIFY_MODE}; sign with `exa models sign`)"
        )
    return local


_REGISTRY_ENTRIES: list | None = None

# Per-model YAML directory (Phase 14) takes precedence over legacy RAY_REGISTRY_PATH.
_ray_models_dir_str = os.getenv("RAY_MODELS_DIR", "").strip()
_ray_registry_path_str = os.getenv("RAY_REGISTRY_PATH", "").strip()

if _ray_models_dir_str:
    try:
        _models_dir = _Path(_ray_models_dir_str)
        if not _models_dir.is_absolute():
            _models_dir = _REPO_ROOT_RS / _models_dir
        if _models_dir.exists():
            from pipelines.model_loader import scan_model_yamls  # noqa: PLC0415

            class _YAMLEntry:
                def __init__(self, name: str, aliases: list[str], enabled: bool) -> None:
                    self.name = name
                    self.serve_aliases = aliases
                    self.enabled = enabled

            _REGISTRY_ENTRIES = [
                _YAMLEntry(
                    c.serving.get("model_id", c.name.lower()),
                    c.serving.get("aliases", PRELOAD_ALIASES),
                    c.enabled,
                )
                for c in scan_model_yamls(_models_dir)
                if c.enabled
            ]
            logger.info(
                "Loaded %d model entries from RAY_MODELS_DIR=%s",
                len(_REGISTRY_ENTRIES),
                _models_dir,
            )
        else:
            logger.warning(
                "RAY_MODELS_DIR=%s does not exist — falling back to RAY_PRELOAD_ALIASES",
                _models_dir,
            )
    except Exception as _reg_exc:
        logger.warning(
            "Could not load RAY_MODELS_DIR %s: %s — falling back to RAY_PRELOAD_ALIASES",
            _ray_models_dir_str,
            _reg_exc,
        )
elif _ray_registry_path_str:
    try:
        from pipelines.registry_loader import load_registry as _load_reg  # noqa: PLC0415

        _reg_env = os.getenv("RAY_REGISTRY_ENV", "").strip()
        _reg_env_path = (
            _Path(_ray_registry_path_str).parent / "envs" / f"{_reg_env}.yaml" if _reg_env else None
        )
        _REGISTRY_ENTRIES = _load_reg(_Path(_ray_registry_path_str), _reg_env_path)
        _REGISTRY_ENTRIES = [e for e in _REGISTRY_ENTRIES if e.enabled]
        logger.info(
            "Loaded %d model entries from %s (legacy RAY_REGISTRY_PATH)",
            len(_REGISTRY_ENTRIES),
            _ray_registry_path_str,
        )
    except Exception as _reg_exc:
        logger.warning(
            "Could not load registry %s: %s — falling back to RAY_PRELOAD_ALIASES",
            _ray_registry_path_str,
            _reg_exc,
        )


def _project_for(model_name: str) -> str | None:
    """Owning Project for a model (ADR 0088), or None. Degrades to None if the platform
    package / DB is unavailable in the serving runtime."""
    try:
        from examlops.project_scope import resolve_project

        return resolve_project(model_name)
    except Exception:  # noqa: BLE001 - per request on the serving path: logging here would
        # emit a line per inference when the datastore is away, drowning the outage itself.
        # Unattributed traffic shows up as the `project` label going absent on the metrics.
        return None


def _get_serve_aliases_for(model_name: str) -> list[str]:
    """Return serve aliases for a model, falling back to PRELOAD_ALIASES.

    Case-insensitive: MLflow stores names as lowercase ('jpcp') but YAML
    entries typically use class name casing ('JPCP').
    """
    if _REGISTRY_ENTRIES:
        model_name_lower = model_name.lower()
        for entry in _REGISTRY_ENTRIES:
            if entry.name.lower() == model_name_lower:
                return entry.serve_aliases
    return PRELOAD_ALIASES


class _StubMV:
    """Stand-in MLflow model-version when the registry doesn't return one.

    ``_load_by_flavour`` only consumes ``.version`` and ``.tags`` from the
    object it receives, so a tiny stub keeps the lazy raw-version path simple
    when ``client.get_model_version`` is unavailable or fails.
    """

    def __init__(
        self,
        version: Any,
        tags: dict[str, str] | None = None,
        signature: dict[str, Any] | None = None,
    ) -> None:
        self.version = version
        # The signature record a serving snapshot carries for this version (plan P4.10).
        self.signature = signature
        self.tags = tags or {}


# ─── Schemas ─────────────────────────────────────────────────────────────────


class PredictRequest(BaseModel):
    """Inference request.

    Resolution order:
      1. ``alias`` (e.g. ``"Staging"``, ``"Canary"``) — load via ``models:/{name}@{alias}``
      2. ``version`` (e.g. ``"3"``) — load a raw, possibly old, version on demand
      3. fall back to the default alias (``MODEL_STAGE``)
    """

    features: dict[str, Any]
    alias: str | None = None
    version: str | None = None


class ModelInfo(BaseModel):
    model_name: str
    alias: str | None
    model_version: str | None
    run_id: str | None
    status: str
    project: str | None = None  # owning Project (ADR 0088), None = unscoped


class PredictResponse(BaseModel):
    model_name: str
    alias: str | None
    model_version: str | None
    run_id: str | None
    prediction: Any


def _oip_error(message: str, status: int) -> JSONResponse:
    """An Open Inference Protocol error: ``{"error": message}`` (not FastAPI's ``detail``)."""
    return JSONResponse({"error": message}, status_code=status)


def _platform_version() -> str:
    try:
        import importlib.metadata as md  # noqa: PLC0415

        return md.version("examlops")
    except Exception:  # noqa: BLE001 - metadata is informational
        return "unknown"


_PLATFORM_VERSION = _platform_version()
# RFC 9745: when /predict was deprecated (2026-09-11), the same instant the control plane uses.
PREDICT_DEPRECATED_AT = "@1789084800"


def _server_scaling() -> dict[str, Any]:
    """Replicas of the model server (plan P4.8): ``RAY_NUM_REPLICAS`` of them, or — with
    ``RAY_AUTOSCALE_MAX_REPLICAS`` set — Ray-native autoscaling between the two, from the
    platform's own policy mapping (``examlops.autoscale``). Every replica holds the whole hot set,
    so the ceiling is a memory decision as much as a throughput one.
    """
    ceiling = os.getenv("RAY_AUTOSCALE_MAX_REPLICAS", "").strip()
    if not ceiling:
        return {"num_replicas": NUM_REPLICAS}
    from examlops.autoscale import AutoscalePolicy, to_ray_autoscaling_config  # noqa: PLC0415

    policy = AutoscalePolicy(
        min_replicas=NUM_REPLICAS,
        max_replicas=max(int(ceiling), NUM_REPLICAS),
        target_value=float(os.getenv("RAY_AUTOSCALE_TARGET_ONGOING", "5")),
        stabilization_s=int(os.getenv("RAY_AUTOSCALE_UPSCALE_DELAY_S", "30")),
        cooldown_s=int(os.getenv("RAY_AUTOSCALE_DOWNSCALE_DELAY_S", "300")),
    )
    return {"autoscaling_config": to_ray_autoscaling_config(policy)}


# ─── Ray Serve deployment ─────────────────────────────────────────────────────

_app = FastAPI(
    title="ExaMLOps Ray Multi-Model Serving",
    version="0.3.0",
    description=(
        "All MLflow-lifecycle-aliased models (Staging / Canary / Production / Archived) "
        "served via Ray Serve. Clients can pick alias or raw version per request; the "
        "deployment hot-reloads on Prefect webhook and a background MLflow poller."
    ),
)


def _is_mlflow_unreachable(exc: BaseException) -> bool:
    """True when *exc* indicates MLflow is down/unreachable (vs. a genuine 404).

    Distinguishes a transport failure (→ surface HTTP 503) from a real
    "alias/version does not exist" (→ 404). Walks the exception chain and matches
    connection/timeout markers rather than MLflow's RESOURCE_DOES_NOT_EXIST code.
    """
    seen = 0
    cur: BaseException | None = exc
    while cur is not None and seen < 8:
        if isinstance(cur, (ConnectionError, TimeoutError)):
            return True
        text = f"{type(cur).__name__}: {cur}".lower()
        if any(
            marker in text
            for marker in (
                "connection",
                "timed out",
                "timeout",
                "max retries",
                "refused",
                "unreachable",
            )
        ):
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False


@serve.deployment(
    **_server_scaling(),
    ray_actor_options={"num_cpus": 1},
    max_ongoing_requests=int(os.getenv("RAY_MAX_ONGOING_REQUESTS", "100")),
    # Load shedding (P4.6): once this many requests wait at a caller, the next gets 503 rather
    # than a place in an ever-longer queue. -1 = unbounded (Ray's default); see "Overload" in
    # docs/components/ray-serve.md for sizing.
    max_queued_requests=int(os.getenv("RAY_MAX_QUEUED_REQUESTS", "-1")),
)
@serve.ingress(_app)
class MultiModelServer:
    def __init__(self) -> None:
        mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)

        # Pre-loaded hot set:
        #   key = (model_name, alias) — alias in PRELOAD_ALIASES
        #   value = {"model": pyfunc, "version": "3", "run_id": "..."}
        self._hot: dict[tuple[str, str], dict[str, Any]] = {}
        # Guards multi-step mutations/iterations of _hot and _version_cache. The
        # background poller thread (_poll_loop → _reload_one_model) and request
        # threads both touch these structures; without this, a snapshot iteration
        # (health/list_models/alias-scan) can race the poller's evict/reassign and
        # raise "dictionary changed size during iteration". Held only around the
        # dict ops themselves — never around slow MLflow loads.
        self._cache_lock = threading.RLock()

        # LRU cache for raw-version requests (e.g. {"version": "1"}):
        #   key = (model_name, version)
        self._version_cache: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        # Per-instance cache size — read from the env-driven module constant
        # at init time. Stored on the instance so tests can override directly
        # (the @serve.ingress wrapper gives methods a frozen copy of module
        # globals, which makes patch.object on the module ineffective inside
        # decorated methods).
        self._version_cache_size = VERSION_CACHE_SIZE
        self._preload_aliases: list[str] = list(PRELOAD_ALIASES)

        # Single-flight guards for cold-alias/version one-shot loads: N concurrent requests for
        # a not-yet-hot (model, alias) must trigger ONE artifact download, not N. Keys are
        # bounded by the registry size × aliases, so the dict never needs eviction.
        self._load_locks: dict[tuple[str, str], threading.Lock] = {}

        self._poll_task: asyncio.Task[None] | None = None
        self._poller_alive = False
        # Bounded pool used to run model.predict() under a hard timeout so one hung
        # inference can't pin the (num_cpus=1) replica worker forever. Stored on the
        # instance (like _version_cache_size) so tests can override — the
        # @serve.ingress wrapper freezes module globals inside decorated methods.
        self._pool_workers = int(os.getenv("RAY_PREDICT_WORKERS", "4"))
        self._predict_pool = ThreadPoolExecutor(
            max_workers=self._pool_workers,
            thread_name_prefix="predict",
        )
        self._predict_timeout = PREDICT_TIMEOUT
        # Poisoned-pool accounting (audit item S6): a timed-out predict whose thread is truly
        # hung permanently consumes a worker — after RAY_PREDICT_WORKERS of them the pool is
        # exhausted and EVERY model on this replica 504s forever while max_ongoing_requests
        # keeps admitting traffic. Track live-hung predicts and recycle the pool when it is
        # fully poisoned, restoring capacity without a replica restart.
        self._pool_lock = threading.Lock()
        self._leaked_predicts = 0
        self._pool_recycles = 0

        # ADR 0024 clause 1 — shadow mirroring. A pool of its own, never `_predict_pool`: a
        # shadow that is slower than the champion must not take threads away from the traffic
        # it is shadowing. In-flight work is capped and excess is dropped rather than queued,
        # so a slow shadow costs a gap in the scoreboard, never memory on a serving replica.
        self._shadow_pool = ThreadPoolExecutor(
            max_workers=int(os.getenv("RAY_SHADOW_WORKERS", "2")),
            thread_name_prefix="shadow",
        )
        self._shadow_max_inflight = int(os.getenv("RAY_SHADOW_MAX_INFLIGHT", "16"))
        self._shadow_ttl = float(os.getenv("RAY_SHADOW_CONFIG_TTL", "30"))
        self._shadow_inflight = 0
        self._shadow_lock = threading.Lock()
        self._shadow_cache: dict[str, tuple[str | None, float]] = {}

        import os as _os

        self._replica_id = _os.getenv("RAY_WORKER_ID", "default")

        # The serving snapshot this replica acts on (ADR 0127): its generation, where it came from,
        # and the shadow targets it carries (which replace the per-request TTL read of the table).
        self._snapshot_reader: SnapshotReader | None = (
            SnapshotReader() if SNAPSHOT_MODE != "off" else None
        )
        self._snapshot_generation: int | None = None
        self._snapshot_shadow: dict[str, Any] | None = None

        # ── Online metrics ────────────────────────────────────────────────────
        # NB: `version` is deliberately NOT a label on the hot-path counter/histogram
        # (cardinality guard, item 3.2/QW5): a new MLflow version would mint a fresh time
        # series on every promotion, multiplied by model × alias × status — a Prometheus
        # OOM bomb. `alias` (Production/Canary/Staging) is bounded and kept; the live
        # version is surfaced as the *value* of `_version_gauge` below (bounded series).
        self._req_counter = Counter(
            "examlops_predict_requests_total",
            description="Total prediction requests by model, alias and outcome",
            tag_keys=("model_name", "alias", "status"),
        )
        self._latency_hist = Histogram(
            "examlops_predict_latency_seconds",
            description="End-to-end prediction latency in seconds by model and alias",
            boundaries=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
            tag_keys=("model_name", "alias"),
        )
        self._version_gauge = Gauge(
            "examlops_model_version",
            description="Live model version currently served per (model, alias) - version as a "
            "gauge VALUE (not a label) so per-version visibility survives the cardinality guard",
            tag_keys=("model_name", "alias"),
        )
        self._pred_value_hist = Histogram(
            "examlops_prediction_value",
            description="Distribution of raw scalar prediction values per model",
            boundaries=[0.1, 10, 50, 100, 200, 500, 1000, 2000, 5000, 10000],
            tag_keys=("model_name",),
        )
        self._models_gauge = Gauge(
            "examlops_models_loaded",
            description="Number of (model, alias) entries in the pre-loaded hot set",
            tag_keys=("replica",),
        )
        # Publish it immediately, before any load is attempted. A gauge that is only written
        # on a successful load does not read as 0 when nothing loaded — it does not exist, and
        # `sum(ray_examlops_models_loaded) == 0` aggregates an absent series to an empty vector
        # that matches nothing. RayServeNoModelsLoaded ("inference is impossible") was therefore
        # silent in exactly the case it is named for. See _load_hot_aliases.
        self._models_gauge.set(len(self._hot), tags={"replica": self._replica_id})
        self._shadow_counter = Counter(
            "examlops_shadow_total",
            description="Shadow mirror outcomes by status (recorded, error, dropped).",
            tag_keys=("model_name", "status"),
        )
        self._reload_counter = Counter(
            "examlops_reload_total",
            description="Number of hot-reload operations",
            tag_keys=("status", "scope", "replica"),
        )
        self._snapshot_gauge = Gauge(
            "examlops_serving_snapshot_applied_generation",
            description="Serving-snapshot generation this replica is serving (ADR 0127), or 0 if "
            "it has applied none; compare with the control plane's "
            "examlops_serving_snapshot_generation for config lag",
            tag_keys=("replica",),
        )
        # ──────────────────────────────────────────────────────────────────────

        if not self._apply_newest_snapshot():
            self._load_hot_aliases()
        self._start_poller()
        self._start_gauge_refresher()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _publish_gauges(self) -> None:
        """Set every gauge from the replica's current state.

        Ray 2.55 exports a gauge value only for the report interval in which it was set. A gauge
        set once, at load or reload, is visible for a few seconds and then gone: the absent() arm
        of RayServeNoModelsLoaded then fires on a healthy replica, ServingSnapshotLagging has no
        applied generation to compare, and the model-version gauge exists only while predictions
        flow. Counters are unaffected. tests/integration/test_serving_metrics_live.py shows both.
        """
        with self._cache_lock:
            entries = [(key, entry.get("version")) for key, entry in self._hot.items()]
        replica = {"replica": self._replica_id}
        self._models_gauge.set(len(entries), tags=replica)
        # 0 means "no snapshot applied", which is a state this gauge has to be able to express:
        # generations are rowids starting at 1, and `ServingSnapshotLagging` is
        # `max(published) - min(applied) > 0`, where `min()` over NO series is an empty vector —
        # so a replica that has never applied a snapshot, the most-behind replica there can be,
        # was invisible to the alert that exists to find it.
        generation = getattr(self, "_snapshot_generation", None)
        self._snapshot_gauge.set(0 if generation is None else generation, tags=replica)
        for (model_name, alias), version in entries:
            try:
                value = float(str(version))
            except ValueError:
                continue  # a non-numeric version has no gauge, as on the predict path
            self._version_gauge.set(value, tags={"model_name": model_name, "alias": alias or ""})

    def _start_gauge_refresher(self) -> None:
        if GAUGE_REFRESH_SECONDS <= 0:
            return

        def _run() -> None:
            while True:
                time.sleep(GAUGE_REFRESH_SECONDS)
                try:
                    self._publish_gauges()
                except Exception:  # noqa: BLE001 - metrics must never take the replica down
                    logger.debug("gauge refresh failed", exc_info=True)

        threading.Thread(target=_run, name="gauge-refresh", daemon=True).start()

    def _load_hot_aliases(self) -> None:
        """Scan MLflow for every (model, alias) in PRELOAD_ALIASES and load it."""
        client = mlflow.MlflowClient()
        try:
            # Retry a transient MLflow blip at boot so a momentary outage doesn't
            # leave this replica permanently empty until the next poll cycle.
            registered = retry_call(
                client.search_registered_models,
                retries=2,
                base_delay=0.5,
                retry_on=lambda e: is_transient_network(e) or _is_mlflow_unreachable(e),
                label="mlflow search_registered_models",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Cannot reach MLflow at %s: %s", MLFLOW_TRACKING_URI, exc)
            # `len(self._hot)`, not 0: the poller calls this again on a live replica, and the
            # early return leaves the existing hot set untouched and still serving from cache.
            # Reporting 0 there would page for an outage that is not happening. At first start
            # the hot set is empty, so this is the 0 the alert needs.
            self._models_gauge.set(len(self._hot), tags={"replica": self._replica_id})
            return

        new_hot: dict[tuple[str, str], dict[str, Any]] = {}
        for rm in registered:
            for alias in _get_serve_aliases_for(rm.name):
                try:
                    mv = client.get_model_version_by_alias(rm.name, alias)
                except Exception:  # noqa: BLE001
                    continue
                try:
                    model = self._load_by_flavour(rm.name, alias, mv)
                    new_hot[(rm.name, alias)] = {
                        "model": model,
                        "version": str(mv.version),
                        "run_id": getattr(getattr(model, "metadata", None), "run_id", None),
                    }
                    logger.info("Loaded '%s'@%s (v%s)", rm.name, alias, mv.version)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to load '%s'@%s: %s", rm.name, alias, exc)
                    # Keep the last-known-good copy (mirrors _reload_one_model): a reload during
                    # an artifact-store outage must not evict a model we're serving healthily
                    # from memory — that would turn "MinIO briefly down" into a total serving
                    # outage triggered by the documented post-promotion runbook step.
                    with self._cache_lock:
                        previous = self._hot.get((rm.name, alias))
                    if previous is not None:
                        new_hot[(rm.name, alias)] = previous
                        logger.warning(
                            "Keeping last-known-good '%s'@%s (v%s) after failed reload",
                            rm.name,
                            alias,
                            previous.get("version"),
                        )

        with self._cache_lock:
            self._hot = new_hot
            hot_keys = list(self._hot.keys())
        self._models_gauge.set(len(hot_keys), tags={"replica": self._replica_id})
        logger.info(
            "Hot set ready — %d entries: %s",
            len(hot_keys),
            sorted(f"{n}@{a}" for (n, a) in hot_keys),
        )

    def _apply_newest_snapshot(self) -> bool:
        """Serve from the newest serving snapshot. False when there is none to serve from.

        True also when the newest generation is the one already applied — the snapshot stays
        authoritative and MLflow is not polled.
        """
        reader = getattr(self, "_snapshot_reader", None)
        if reader is None:
            return False
        snapshot = reader.newest()
        if snapshot is None:
            return False
        if snapshot["generation"] != getattr(self, "_snapshot_generation", None):
            self._apply_snapshot(snapshot)
        return True

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Make the hot set match ``snapshot``: load what moved, keep what did not, drop what left.

        Models are loaded **by version** (``models:/name/7``), never by alias: the alias can move
        again between the snapshot and the download, and the replica must serve what the snapshot
        says. A load that fails keeps the last-known-good entry, as the MLflow path does.
        """
        models = snapshot.get("models", {})
        with self._cache_lock:
            current = dict(self._hot)
        new_hot: dict[tuple[str, str], dict[str, Any]] = {}
        moved: set[str] = set()
        for entry in models.values():
            name = entry["name"]
            aliases = entry.get("aliases", {})
            wanted = set(_get_serve_aliases_for(name))
            for alias, facts in aliases.items():
                previous = current.get((name, alias))
                schema = facts.get("input_schema")
                if previous is not None and previous["version"] == facts["version"]:
                    # Same version, but the schema comes from the snapshot: follow it.
                    new_hot[(name, alias)] = {**previous, "input_schema": schema}
                    continue
                if alias not in wanted:
                    continue  # a cold alias whose version moved is reloaded on its next request
                try:
                    mv = _StubMV(
                        facts["version"],
                        {"framework": facts.get("framework", "sklearn")},
                        signature=facts.get("signature"),
                    )
                    new_hot[(name, alias)] = {
                        "model": self._load_by_flavour(name, None, mv),
                        "version": str(facts["version"]),
                        "run_id": facts.get("run_id"),
                        "input_schema": schema,
                    }
                    moved.add(name)
                    logger.info("Loaded '%s'@%s (v%s) from snapshot", name, alias, facts["version"])
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "Failed to load '%s'@%s v%s: %s", name, alias, facts["version"], exc
                    )
                    if previous is not None:
                        new_hot[(name, alias)] = previous
        with self._cache_lock:
            self._hot = new_hot
            for k in [k for k in self._version_cache if k[0] in moved]:
                self._version_cache.pop(k, None)
            hot_count = len(new_hot)
        self._snapshot_shadow = {
            key: str(value.get("alias"))
            for key, value in snapshot.get("shadow", {}).items()
            if value.get("alias")
        }
        self._snapshot_generation = int(snapshot["generation"])
        self._models_gauge.set(hot_count, tags={"replica": self._replica_id})
        self._snapshot_gauge.set(self._snapshot_generation, tags={"replica": self._replica_id})
        logger.info(
            "Serving snapshot generation %d applied (%d entries, source=%s)",
            self._snapshot_generation,
            hot_count,
            getattr(self._snapshot_reader, "source", None),
        )

    def _load_by_flavour(self, name: str, alias: str | None, mv: Any) -> Any:
        """Load the right MLflow flavour based on the model-version's ``framework`` tag.

        Tag absent ⇒ sklearn / pyfunc (legacy default). Phase 5 sets the tag
        in ``log_mlflow_task`` so non-sklearn models load via their flavour
        without any per-model special-casing in serving.
        """
        # Use importlib so we don't accidentally rebind the module-level
        # ``mlflow`` name as a function-local (``import mlflow.pytorch`` would).
        import importlib  # noqa: PLC0415

        flavour = "sklearn"
        try:
            tags = getattr(mv, "tags", {}) or {}
            flavour = (tags.get("framework") or "sklearn").lower()
        except Exception:  # noqa: BLE001
            pass

        suffix = f"@{alias}" if alias else f"/{mv.version}"
        uri = _verified_uri(
            name,
            str(mv.version),
            f"models:/{name}{suffix}",
            record=getattr(mv, "signature", None),
        )

        if flavour == "pytorch":
            return importlib.import_module("mlflow.pytorch").load_model(uri)
        if flavour == "huggingface":
            return importlib.import_module("mlflow.transformers").load_model(uri)
        return mlflow.pyfunc.load_model(uri)

    def _reload_one_model(self, model_name: str) -> int:
        """Re-pull every preload alias for a single model. Returns reload count."""
        client = mlflow.MlflowClient()
        reloaded = 0
        for alias in _get_serve_aliases_for(model_name):
            try:
                mv = client.get_model_version_by_alias(model_name, alias)
            except Exception as exc:  # noqa: BLE001
                # Only evict when the alias is genuinely gone. A transient MLflow
                # blip must NOT pull a healthy model out of rotation — keep serving
                # the last-known-good entry until a later poll confirms the change.
                if _is_mlflow_unreachable(exc):
                    logger.warning(
                        "Skipping reload of '%s'@%s — MLflow unreachable: %s",
                        model_name,
                        alias,
                        exc,
                    )
                else:
                    with self._cache_lock:
                        self._hot.pop((model_name, alias), None)
                continue
            try:
                model = self._load_by_flavour(model_name, alias, mv)
                with self._cache_lock:
                    self._hot[(model_name, alias)] = {
                        "model": model,
                        "version": str(mv.version),
                        "run_id": getattr(getattr(model, "metadata", None), "run_id", None),
                    }
                reloaded += 1
                logger.info("Reloaded '%s'@%s (v%s)", model_name, alias, mv.version)
            except Exception as exc:  # noqa: BLE001
                logger.error("Reload of '%s'@%s failed: %s", model_name, alias, exc)
        # Drop any cached raw versions for this model so subsequent
        # version-pinned requests pick up the fresh artefact.
        with self._cache_lock:
            for k in [k for k in self._version_cache if k[0] == model_name]:
                self._version_cache.pop(k, None)
        self._models_gauge.set(len(self._hot), tags={"replica": self._replica_id})
        return reloaded

    def _start_poller(self) -> None:
        """Launch the background MLflow poller (no-op when RELOAD_POLL_SECONDS=0).

        Prefers the replica's running event loop; if ``__init__`` runs outside one
        (the old ``get_event_loop()`` path silently created a loop that never ran,
        so the poller never fired), falls back to a dedicated daemon thread with its
        own loop. Either way ``_poller_alive`` is set so ``/health`` can surface a
        dead poller as a watchdog signal.
        """
        if RELOAD_POLL_SECONDS <= 0 and getattr(self, "_snapshot_reader", None) is None:
            logger.info("RAY_RELOAD_POLL_SECONDS=0 and RAY_SNAPSHOT_MODE=off — polling disabled.")
            return
        try:
            loop = asyncio.get_running_loop()
            self._poll_task = loop.create_task(self._poll_loop())
            self._poller_alive = True
            logger.info(
                "MLflow alias poller started on running loop (interval=%ss)", RELOAD_POLL_SECONDS
            )
            return
        except RuntimeError:
            pass

        def _run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._poller_alive = True
            try:
                loop.run_until_complete(self._poll_loop())
            finally:
                self._poller_alive = False
                loop.close()

        threading.Thread(target=_run_loop, name="mlflow-poller", daemon=True).start()
        logger.info(
            "MLflow alias poller started on dedicated thread (interval=%ss)", RELOAD_POLL_SECONDS
        )

    async def _poll_loop(self) -> None:
        """Follow the serving snapshot; without one, refresh entries whose MLflow version moved.

        With a snapshot the loop wakes every ``RAY_SNAPSHOT_POLL_SECONDS`` and MLflow is not
        polled at all. Only while no snapshot exists does it fall back to scanning MLflow every
        ``RAY_RELOAD_POLL_SECONDS``.
        """
        client = mlflow.MlflowClient()
        snapshots = getattr(self, "_snapshot_reader", None) is not None
        tick = min(SNAPSHOT_POLL_SECONDS, RELOAD_POLL_SECONDS) if snapshots else RELOAD_POLL_SECONDS
        if tick <= 0:
            tick = SNAPSHOT_POLL_SECONDS
        next_scan = time.monotonic() + RELOAD_POLL_SECONDS
        while True:
            try:
                await asyncio.sleep(tick)
                if snapshots and await asyncio.to_thread(self._apply_newest_snapshot):
                    continue
                if RELOAD_POLL_SECONDS <= 0 or time.monotonic() < next_scan:
                    continue
                next_scan = time.monotonic() + RELOAD_POLL_SECONDS
                changes = self._detect_alias_changes(client)
                if not changes:
                    continue
                for model_name in changes:
                    self._reload_one_model(model_name)
                self._reload_counter.inc(
                    tags={"status": "success", "scope": "poll", "replica": self._replica_id}
                )
            except asyncio.CancelledError:
                logger.info("Poller cancelled.")
                return
            except Exception as exc:  # noqa: BLE001
                logger.error("Poller iteration failed: %s", exc)
                self._reload_counter.inc(
                    tags={"status": "error", "scope": "poll", "replica": self._replica_id}
                )

    def _detect_alias_changes(self, client: mlflow.MlflowClient) -> set[str]:
        """Return the set of model names whose hot-alias versions diverged from MLflow."""
        observed: dict[tuple[str, str], str | None] = {}
        try:
            for rm in client.search_registered_models():
                for alias in _get_serve_aliases_for(rm.name):
                    try:
                        mv = client.get_model_version_by_alias(rm.name, alias)
                        observed[(rm.name, alias)] = str(mv.version)
                    except Exception:  # noqa: BLE001
                        observed[(rm.name, alias)] = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("alias-change scan failed: %s", exc)
            return set()

        changed: set[str] = set()
        # Versions that moved or aliases that disappeared.
        with self._cache_lock:
            hot_snapshot = list(self._hot.items())
        for key, hot_entry in hot_snapshot:
            if observed.get(key) != hot_entry["version"]:
                changed.add(key[0])
        # Brand-new (model, alias) pairs that we haven't loaded yet.
        for key, version in observed.items():
            if version is not None and key not in self._hot:
                changed.add(key[0])
        return changed

    @staticmethod
    def _snapshot_signature(resolved: dict[str, Any]) -> oip.Signature | None:
        """The signature the serving snapshot gave this entry, or None (unchecked / off)."""
        # Read per call (an env lookup): the ingress wrapper freezes module globals in methods.
        if os.getenv("RAY_INPUT_SCHEMA", "enforce").strip().lower() == "off":
            return None
        return oip.signature_from_schema(resolved.get("input_schema"))

    def _hot_get(self, model_name: str, alias: str) -> dict[str, Any] | None:
        """Look up the hot set, tolerating model-name case.

        The hot set is keyed on the MLflow registered-model name, which is lowercase
        by convention (``jpcp``). A direct client may POST the canonical uppercase
        name (``JPCP``); try the exact key first, then the lowercase key, so such a
        request doesn't 404 a model that is in fact loaded.
        """
        entry = self._hot.get((model_name, alias))
        if entry is None and model_name != model_name.lower():
            entry = self._hot.get((model_name.lower(), alias))
        return entry

    def _note_predict_timeout(self, future: Any) -> None:
        """Account a timed-out predict; recycle the pool once it is fully poisoned (S6).

        ``cancel()`` succeeding means the task never started — the timeout was queue
        congestion, no thread is stuck, nothing leaks. Otherwise the call is running (possibly
        hung): count it, and give it a done-callback so a merely-slow call that eventually
        returns gives its slot back to the accounting. Lazy attribute init so hand-built test
        instances (object.__new__) work.
        """
        if future.cancel():
            return
        lock = getattr(self, "_pool_lock", None)
        if lock is None:
            lock = self._pool_lock = threading.Lock()
            self._leaked_predicts = 0
            self._pool_recycles = 0
            self._pool_workers = int(os.getenv("RAY_PREDICT_WORKERS", "4"))
        with lock:
            self._leaked_predicts += 1
            leaked = self._leaked_predicts

        def _release(_f: Any) -> None:
            with lock:
                self._leaked_predicts = max(0, self._leaked_predicts - 1)

        future.add_done_callback(_release)
        if leaked >= self._pool_workers:
            self._recycle_predict_pool()

    def _recycle_predict_pool(self) -> None:
        """Swap in a fresh predict pool; the old one (all workers hung) is abandoned.

        The hung threads themselves cannot be killed from Python — they stay until their C
        calls return — but the REPLICA recovers: new requests get fresh workers instead of
        queueing behind a dead pool until the 504-everything state. Each recycle is logged
        loudly; recurring recycles are the signal to fix the model that hangs.
        """
        with self._pool_lock:
            if self._leaked_predicts < self._pool_workers:
                return  # another request already recycled
            old_pool = self._predict_pool
            self._predict_pool = ThreadPoolExecutor(
                max_workers=self._pool_workers, thread_name_prefix="predict"
            )
            self._leaked_predicts = 0
            self._pool_recycles += 1
            recycle_n = self._pool_recycles
        old_pool.shutdown(wait=False)
        logger.error(
            "Predict pool POISONED (%d hung predicts) — recycled into a fresh pool "
            "(recycle #%d). The hung threads remain until their calls return; a recurring "
            "recycle means a model reproducibly hangs and needs fixing.",
            self._pool_workers,
            recycle_n,
        )

    def _single_flight(self, model_name: str, key2: str) -> threading.Lock:
        """Per-(model, alias-or-version) load lock. Lazy so hand-built test instances work."""
        with self._cache_lock:
            locks = getattr(self, "_load_locks", None)
            if locks is None:
                locks = self._load_locks = {}
            return locks.setdefault((model_name, key2), threading.Lock())

    def _resolve(self, model_name: str, alias: str | None, version: str | None) -> dict[str, Any]:
        """Return ``{"model", "version", "alias", "run_id"}`` for the request.

        Raises HTTPException(404) when the requested combo can't be loaded.
        """
        # 1. Alias takes precedence over raw version.
        if alias:
            entry = self._hot_get(model_name, alias)
            if entry is not None:
                return {**entry, "alias": alias}
            # Alias not in the hot set — try a one-shot load, single-flight per key so
            # concurrent cold requests share one download instead of a thundering herd.
            try:
                with self._single_flight(model_name, alias):
                    entry = self._hot_get(model_name, alias)
                    if entry is not None:  # loaded by the request we waited on
                        return {**entry, "alias": alias}
                    client = mlflow.MlflowClient()
                    # Any: reused below for the raw-version path where it may be None.
                    mv: Any = client.get_model_version_by_alias(model_name, alias)
                    model = self._load_by_flavour(model_name, alias, mv)
                    entry = {
                        "model": model,
                        "version": str(mv.version),
                        "run_id": getattr(getattr(model, "metadata", None), "run_id", None),
                    }
                    with self._cache_lock:
                        self._hot[(model_name, alias)] = entry
                    return {**entry, "alias": alias}
            except Exception as exc:  # noqa: BLE001
                if _is_mlflow_unreachable(exc):
                    raise HTTPException(
                        status_code=503,
                        detail=f"MLflow unreachable while resolving '{model_name}'@{alias}: {exc}",
                    ) from exc
                raise HTTPException(
                    status_code=404,
                    detail=f"Alias '{alias}' not found for model '{model_name}': {exc}",
                ) from exc

        # 2. Raw version — LRU cache.
        if version:
            key = (model_name, str(version))
            with self._cache_lock:
                entry = self._version_cache.get(key)
                if entry is not None:
                    self._version_cache.move_to_end(key)
            if entry is not None:
                return {**entry, "alias": None}
            try:
                with self._single_flight(model_name, f"v{version}"):
                    with self._cache_lock:
                        entry = self._version_cache.get(key)
                        if entry is not None:  # loaded by the request we waited on
                            self._version_cache.move_to_end(key)
                    if entry is not None:
                        return {**entry, "alias": None}
                    client = mlflow.MlflowClient()
                    try:
                        mv = client.get_model_version(model_name, str(version))
                    except Exception:  # noqa: BLE001
                        mv = None
                    model = self._load_by_flavour(model_name, None, mv or _StubMV(version))
                    entry = {
                        "model": model,
                        "version": str(version),
                        "run_id": getattr(getattr(model, "metadata", None), "run_id", None),
                    }
                    with self._cache_lock:
                        self._version_cache[key] = entry
                        self._version_cache.move_to_end(key)
                        while len(self._version_cache) > self._version_cache_size:
                            self._version_cache.popitem(last=False)
                    return {**entry, "alias": None}
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                if _is_mlflow_unreachable(exc):
                    raise HTTPException(
                        status_code=503,
                        detail=f"MLflow unreachable while resolving '{model_name}' v{version}: {exc}",
                    ) from exc
                raise HTTPException(
                    status_code=404,
                    detail=f"Version '{version}' not found for model '{model_name}': {exc}",
                ) from exc

        # 3. Default alias (MODEL_STAGE).
        entry = self._hot_get(model_name, MODEL_STAGE)
        if entry is None:
            with self._cache_lock:
                available = sorted({n for (n, _) in self._hot})
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Model '{model_name}' has no '{MODEL_STAGE}' alias loaded. "
                    f"Available: {available}"
                ),
            )
        return {**entry, "alias": MODEL_STAGE}

    # ── Routes ────────────────────────────────────────────────────────────────

    @_app.get("/ready")
    def ready(self) -> dict:
        """Liveness — the process is up and event loop responsive. Always 200.

        Container/orchestrator health checks should target this so a transient
        MLflow outage (empty hot set) does NOT trigger a restart loop; use
        ``/health`` for readiness/routing decisions.
        """
        return {"status": "alive", "poller_alive": self._poller_alive}

    @_app.get("/health")
    def health(self, response: Response) -> dict:
        """Readiness — reports every (model, alias) held. HTTP 503 when degraded.

        Degraded = empty hot set (MLflow unreachable at boot or all loads failed),
        so load balancers / monitoring stop routing to a replica that cannot serve.
        """
        with self._cache_lock:
            hot_items = list(self._hot.items())
            version_cache_size = len(self._version_cache)
        degraded = not hot_items
        if degraded:
            response.status_code = 503
        return {
            "status": "degraded" if degraded else "ok",
            "models_loaded": len(hot_items),
            "version_cache_size": version_cache_size,
            "poller_alive": self._poller_alive,
            "poll_interval_s": RELOAD_POLL_SECONDS,
            "snapshot": {
                "mode": SNAPSHOT_MODE,
                "generation": getattr(self, "_snapshot_generation", None),
                "source": getattr(getattr(self, "_snapshot_reader", None), "source", None),
            },
            "models": [
                {
                    "model_name": name,
                    "alias": alias,
                    "version": entry["version"],
                    "run_id": entry["run_id"],
                    "status": "ok",
                }
                for (name, alias), entry in hot_items
            ],
        }

    @_app.get("/models", response_model=list[ModelInfo])
    def list_models(self) -> list[ModelInfo]:
        """Every (model, alias) pair currently in the hot set."""
        with self._cache_lock:
            hot_items = list(self._hot.items())
        return [
            ModelInfo(
                model_name=name,
                alias=alias,
                model_version=entry["version"],
                run_id=entry["run_id"],
                status="ok",
                project=_project_for(name),
            )
            for (name, alias), entry in hot_items
        ]

    # ── ADR 0024 clause 1 — shadow mirroring ─────────────────────────────────

    def _shadow_target(self, model_name: str) -> str | None:
        """The alias to mirror ``model_name`` to, or ``None``. Cached, and never raising.

        Read through a short TTL cache because this is consulted on **every** request and the
        answer changes when an operator runs `exa serve shadow enable` — minutes, not
        milliseconds. A per-request SQLite read would put the shadow feature's cost on the
        production path, which is the one thing clause 1 forbids.

        Any failure — no database, no table, a locked file — returns ``None``. A shadow that
        cannot read its own configuration must look exactly like a shadow that is switched off.
        """
        snapshot_shadow = getattr(self, "_snapshot_shadow", None)
        if snapshot_shadow is not None:  # the snapshot carries it: no database read at all
            return snapshot_shadow.get(model_name.lower())
        now = time.time()
        cached = self._shadow_cache.get(model_name)
        if cached is not None and now - cached[1] < self._shadow_ttl:
            return cached[0]
        target: str | None = None
        try:
            from examlops.platform_db import get_db

            with get_db() as conn:
                # Case-insensitive: the CLI and dashboard store the canonical lowercase key, but a
                # direct `/predict/JPCP` call names the model as typed (plan P0.4 / finding B4).
                row = conn.execute(
                    "SELECT shadow_alias, enabled FROM shadow_config WHERE lower(model)=lower(?) "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (model_name,),
                ).fetchone()
            if row and row["enabled"]:
                target = str(row["shadow_alias"])
        except Exception:
            target = None
        self._shadow_cache[model_name] = (target, now)
        return target

    def _run_shadow(
        self, model_name: str, shadow_alias: str, input_array: Any, production_pred: Any
    ) -> None:
        """Predict with the shadow model and record the comparison. Never raises.

        Runs on the shadow pool, off the request thread. Everything here is best-effort by
        construction: this function's contract is that no outcome of it — a missing alias, a model
        that throws, an unwritable database — can reach the caller, because the caller has already
        returned a production response.
        """
        try:
            resolved = self._resolve(model_name, shadow_alias, None)
            raw = resolved["model"].predict(input_array)
            pred: Any = raw.tolist() if hasattr(raw, "tolist") else raw
            if isinstance(pred, list) and len(pred) == 1:
                pred = pred[0]
            if not isinstance(pred, (int, float)) or not isinstance(production_pred, (int, float)):
                # `shadow_results` stores REAL columns and a percentage difference. A
                # non-numeric prediction has no diff to compute, and writing NULLs would put
                # rows in the scoreboard that no comparison can ever use.
                return
            prod = float(production_pred)
            shadow = float(pred)
            diff_pct = ((shadow - prod) / abs(prod) * 100.0) if prod else None
            from examlops.platform_db import get_db

            with get_db() as conn:
                conn.execute(
                    """INSERT INTO shadow_results (model, production_pred, shadow_pred, diff_pct)
                       VALUES (?,?,?,?)""",
                    (model_name, prod, shadow, diff_pct),
                )
            self._shadow_counter.inc(tags={"model_name": model_name, "status": "recorded"})
        except Exception as exc:  # noqa: BLE001 - a shadow must never surface anywhere
            logger.debug("shadow mirror failed for %s: %s", model_name, exc)
            try:
                self._shadow_counter.inc(tags={"model_name": model_name, "status": "error"})
            except Exception:
                pass

    def _mirror(self, model_name: str, input_array: Any, production_pred: Any) -> None:
        """Fire-and-forget the shadow request (ADR 0024 clause 1). Never raises, never waits.

        Three properties this ADR asks for, and the way each is obtained:

        * **Asynchronous** — submitted to a pool the production path never joins on. `submit`
          returns immediately; the future is deliberately discarded.
        * **Never returned** — the caller has the champion's response already; this writes to
          `shadow_results` and gives nothing back.
        * **Failures never affect production** — the pool is separate from `_predict_pool`, so a
          slow shadow cannot starve inference of its threads, and every path here swallows.

        The queue is **bounded and drops when full**. An unbounded one would turn a shadow model
        that is merely slower than the champion into unbounded memory growth on a production
        replica — the shadow outliving the traffic that produced it, at the expense of the traffic
        that follows. A dropped sample is counted, because a scoreboard silently built from the
        requests that happened to fit would misrepresent the comparison it exists to make.
        """
        shadow_alias = self._shadow_target(model_name)
        if not shadow_alias:
            return
        with self._shadow_lock:
            if self._shadow_inflight >= self._shadow_max_inflight:
                try:
                    self._shadow_counter.inc(tags={"model_name": model_name, "status": "dropped"})
                except Exception:
                    pass
                return
            self._shadow_inflight += 1

        def _task() -> None:
            try:
                self._run_shadow(model_name, shadow_alias, input_array, production_pred)
            finally:
                with self._shadow_lock:
                    self._shadow_inflight -= 1

        try:
            self._shadow_pool.submit(_task)
        except Exception:  # pool shutting down — drop, never raise into the request
            with self._shadow_lock:
                self._shadow_inflight -= 1

    def _run_model(
        self,
        model_name: str,
        alias: str | None,
        version: str | None,
        model: Any,
        input_array: Any,
        deadline: Deadline,
    ) -> Any:
        """Run ``model`` on ``input_array`` for either protocol: under the replica's hard timeout
        or the caller's shorter budget, with the request metrics, the latency histogram and the
        shadow mirror. Returns the prediction as a list (one entry per row). Raises
        HTTPException 504 on a timeout and 500 when the model raises.
        """
        _t0 = time.time()
        # The caller's remaining budget, when shorter than this replica's own hard limit.
        timeout = min(self._predict_timeout, deadline.remaining())
        budget_limited = timeout < self._predict_timeout
        try:
            # Run under a hard timeout so a hung model can't pin the replica worker.
            _predict_future = self._predict_pool.submit(model.predict, input_array)
            raw = _predict_future.result(timeout=timeout)
            result: Any = raw.tolist() if hasattr(raw, "tolist") else raw
        except FuturesTimeoutError as exc:
            if budget_limited:
                # The caller gave up, not the model: a merely-slow call is not a hung thread, and
                # counting it as one would recycle a healthy pool under a tight client budget.
                _predict_future.cancel()
            else:
                self._note_predict_timeout(_predict_future)
            self._req_counter.inc(
                tags={
                    "model_name": model_name,
                    "alias": alias or "",
                    "status": "deadline_exceeded" if budget_limited else "timeout",
                }
            )
            self._latency_hist.observe(
                time.time() - _t0,
                tags={"model_name": model_name, "alias": alias or ""},
            )
            logger.error(
                "Prediction TIMEOUT (>%.3fs%s) for '%s' v%s",
                timeout,
                ", caller's budget" if budget_limited else "",
                model_name,
                version,
            )
            raise HTTPException(
                status_code=504, detail=f"Prediction timed out after {timeout:.3g}s"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            self._req_counter.inc(
                tags={
                    "model_name": model_name,
                    "alias": alias or "",
                    "status": "error",
                }
            )
            self._latency_hist.observe(
                time.time() - _t0,
                tags={"model_name": model_name, "alias": alias or ""},
            )
            logger.error("Prediction error for '%s' (v%s): %s", model_name, version, exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        _latency = time.time() - _t0
        self._req_counter.inc(
            tags={
                "model_name": model_name,
                "alias": alias or "",
                "status": "success",
            }
        )
        self._latency_hist.observe(_latency, tags={"model_name": model_name, "alias": alias or ""})
        # Surface the live version as a bounded gauge value (cardinality-safe, item 3.2/QW5).
        if version is not None:
            try:
                self._version_gauge.set(
                    float(version), tags={"model_name": model_name, "alias": alias or ""}
                )
            except (TypeError, ValueError):
                pass  # non-numeric version (rare) — skip the gauge, metrics still flow
        single = result[0] if isinstance(result, list) and len(result) == 1 else result
        if isinstance(single, (int, float)):
            self._pred_value_hist.observe(float(single), tags={"model_name": model_name})

        logger.info(
            "predict | model=%s alias=%s v%s → %s (%.3fs)",
            model_name,
            alias,
            version,
            single if input_array.shape[0] == 1 else f"<{input_array.shape[0]} rows>",
            _latency,
        )
        # ADR 0024 clause 1. Last thing before returning, and only on the success path: a
        # request that 4xx'd or timed out has no champion prediction to compare against, so
        # mirroring it would add a shadow row with nothing on the other side of it. Fire-and-
        # forget — `_mirror` never waits, never raises, and never touches this response.
        if input_array.shape[0] == 1:  # a shadow compares one champion prediction, not a batch
            self._mirror(model_name, input_array, single)
        return result

    @_app.post("/predict/{model_name}", response_model=PredictResponse)
    def predict(
        self,
        model_name: str,
        request: PredictRequest,
        budget_ms: Annotated[str | None, Header(alias=BUDGET_HEADER)] = None,
        response: Response = None,  # type: ignore[assignment]  # FastAPI injects it
    ) -> PredictResponse:
        """Run inference for *model_name* with optional alias / version selection.

        Deprecated in favour of Open Inference Protocol v2 (ADR 0126): every answer carries RFC 9745
        ``Deprecation`` and an RFC 8288 ``Link`` to ``/v2/models/{name}/infer``, as the control
        plane's legacy routes do. No removal date is set yet, so there is no ``Sunset``.

        ``X-ExaMLOps-Budget-Ms`` (sent by the inference router, optional for direct callers) is
        the time the caller still waits. The model runs under the smaller of that and
        ``RAY_PREDICT_TIMEOUT``, and a request whose budget ran out while it was queued is
        answered 504 without running the model at all (P4.6).
        """
        if response is not None:
            response.headers["Deprecation"] = PREDICT_DEPRECATED_AT
            response.headers["Link"] = f'</v2/models/{model_name}/infer>; rel="successor-version"'
        deadline = Deadline.from_budget_ms(budget_ms, default=self._predict_timeout)
        if deadline.expired():
            self._req_counter.inc(
                tags={
                    "model_name": model_name,
                    "alias": request.alias or "",
                    "status": "deadline_exceeded",
                }
            )
            raise HTTPException(status_code=504, detail="deadline exceeded before the model ran")
        try:
            resolved = self._resolve(model_name, request.alias, request.version)
        except HTTPException:
            self._req_counter.inc(
                tags={
                    "model_name": model_name,
                    "alias": request.alias or "",
                    "status": "not_found",
                }
            )
            raise

        model = resolved["model"]
        version = resolved["version"]
        alias = resolved["alias"]

        import numpy as np

        # Build + validate the feature vector *before* timing/predicting so a
        # non-numeric feature is a clean 422 (client error), not an opaque 500
        # that also pollutes the error-rate metric.
        # A model with a column signature gets its features by name, in the signature's order:
        # MLflow refuses an unnamed array for it, so /predict answered 500 on every such model.
        snapshot_signature = self._snapshot_signature(resolved)
        if snapshot_signature is not None:
            try:
                oip.check_features(request.features, snapshot_signature)
            except oip.ProtocolError as exc:
                self._req_counter.inc(
                    tags={"model_name": model_name, "alias": alias or "", "status": "invalid"}
                )
                raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        signature = snapshot_signature or oip.signature_of(model)
        row: list = []
        if signature is not None and signature.columns:
            missing = [n for n in signature.names if n not in request.features]
            if missing:
                self._req_counter.inc(
                    tags={"model_name": model_name, "alias": alias or "", "status": "invalid"}
                )
                raise HTTPException(
                    status_code=422,
                    detail=f"missing features {missing}; the model takes {list(signature.names)}",
                )
            row = [request.features[n] for n in signature.names]
        else:
            for v in request.features.values():
                if isinstance(v, list):
                    row.extend(v)
                else:
                    row.append(v)
        try:
            input_array = np.array([row], dtype=float)
        except (ValueError, TypeError) as exc:
            self._req_counter.inc(
                tags={
                    "model_name": model_name,
                    "alias": alias or "",
                    "status": "invalid",
                }
            )
            raise HTTPException(status_code=422, detail=f"features must be numeric: {exc}") from exc

        prediction = self._run_model(
            model_name, alias, version, model, oip.model_input(input_array, signature), deadline
        )
        if isinstance(prediction, list) and len(prediction) == 1:
            prediction = prediction[0]
        return PredictResponse(
            model_name=model_name,
            alias=alias,
            model_version=version,
            run_id=resolved["run_id"],
            prediction=prediction,
        )

    # ── Open Inference Protocol v2 (plan P4.5, ADRs 0126 and 0141) ───────────────────────────
    # The same models and the same execution path as /predict, spoken in the protocol KServe,
    # Triton and MLServer clients use. Errors are {"error": ...}; health is 200 (true) or 4xx
    # (false) with an empty body, as the protocol specifies.

    @_app.get("/v2")
    def v2_server_metadata(self) -> dict[str, Any]:
        return {"name": "examlops-ray-serving", "version": _PLATFORM_VERSION, "extensions": []}

    @_app.get("/v2/health/live")
    def v2_health_live(self) -> Response:
        return Response(status_code=200)

    @_app.get("/v2/health/ready")
    def v2_health_ready(self) -> Response:
        with self._cache_lock:
            loaded = bool(self._hot)
        return Response(status_code=200 if loaded else 400)

    @_app.get("/v2/models/{model_name}")
    def v2_model_metadata(self, model_name: str) -> Any:
        return self._v2_metadata(model_name, None)

    @_app.get("/v2/models/{model_name}/versions/{version}")
    def v2_model_version_metadata(self, model_name: str, version: str) -> Any:
        return self._v2_metadata(model_name, version)

    @_app.get("/v2/models/{model_name}/ready")
    def v2_model_ready(self, model_name: str) -> Response:
        return Response(status_code=200 if self._v2_is_ready(model_name, None) else 400)

    @_app.get("/v2/models/{model_name}/versions/{version}/ready")
    def v2_model_version_ready(self, model_name: str, version: str) -> Response:
        return Response(status_code=200 if self._v2_is_ready(model_name, version) else 400)

    @_app.post("/v2/models/{model_name}/infer")
    def v2_infer(
        self,
        model_name: str,
        body: Any = Body(default=None),
        budget_ms: Annotated[str | None, Header(alias=BUDGET_HEADER)] = None,
    ) -> Any:
        return self._v2_infer(model_name, None, body, budget_ms)

    @_app.post("/v2/models/{model_name}/versions/{version}/infer")
    def v2_version_infer(
        self,
        model_name: str,
        version: str,
        body: Any = Body(default=None),
        budget_ms: Annotated[str | None, Header(alias=BUDGET_HEADER)] = None,
    ) -> Any:
        return self._v2_infer(model_name, version, body, budget_ms)

    def _v2_is_ready(self, model_name: str, version: str | None) -> bool:
        """Loaded and able to infer now. A readiness probe never triggers a model download."""
        if version is None:
            return self._hot_get(model_name, MODEL_STAGE) is not None
        with self._cache_lock:
            if (model_name, str(version)) in self._version_cache:
                return True
            return any(
                n.lower() == model_name.lower() and str(e.get("version")) == str(version)
                for (n, _), e in self._hot.items()
            )

    def _v2_metadata(self, model_name: str, version: str | None) -> Any:
        try:
            resolved = self._resolve(model_name, None, version)
        except HTTPException as exc:
            return _oip_error(str(exc.detail), exc.status_code)
        return oip.model_metadata(
            model_name,
            [str(resolved["version"])],
            "mlflow",
            self._snapshot_signature(resolved) or oip.signature_of(resolved["model"]),
        )

    def _v2_infer(self, model_name: str, version: str | None, body: Any, budget_ms: Any) -> Any:
        params = oip.infer_parameters(body)
        requested_alias = params.get("alias") if isinstance(params.get("alias"), str) else None
        alias = None if version is not None else requested_alias
        deadline = Deadline.from_budget_ms(budget_ms, default=self._predict_timeout)
        if deadline.expired():
            self._req_counter.inc(
                tags={"model_name": model_name, "alias": alias or "", "status": "deadline_exceeded"}
            )
            return _oip_error("deadline exceeded before the model ran", 504)
        try:
            resolved = self._resolve(model_name, alias, version)
        except HTTPException as exc:
            self._req_counter.inc(
                tags={"model_name": model_name, "alias": alias or "", "status": "not_found"}
            )
            return _oip_error(str(exc.detail), exc.status_code)
        signature = self._snapshot_signature(resolved) or oip.signature_of(resolved["model"])
        try:
            array = oip.to_array(body, signature)
        except oip.ProtocolError as exc:
            self._req_counter.inc(
                tags={
                    "model_name": model_name,
                    "alias": resolved["alias"] or "",
                    "status": "invalid",
                }
            )
            return _oip_error(str(exc), exc.status)
        try:
            result = self._run_model(
                model_name,
                resolved["alias"],
                resolved["version"],
                resolved["model"],
                oip.model_input(array, signature),
                deadline,
            )
        except HTTPException as exc:
            return _oip_error(str(exc.detail), exc.status_code)
        outputs = body.get("outputs") if isinstance(body, dict) else None
        output_name = "predict"
        if isinstance(outputs, list) and outputs and isinstance(outputs[0], dict):
            output_name = str(outputs[0].get("name") or "predict")
        return oip.response(
            model_name,
            str(resolved["version"]) if resolved["version"] is not None else None,
            result,
            request_id=body.get("id") if isinstance(body.get("id"), str) else None,
            output_name=output_name,
            parameters={
                k: v
                for k, v in (("alias", resolved["alias"]), ("run_id", resolved.get("run_id")))
                if v
            },
        )

    # Admin routes need RAY_SERVE_ADMIN_TOKEN (plan P0.6 / finding S1); /predict does not.
    @_app.post("/reload", dependencies=[Depends(require_serving_admin)])
    def reload(self) -> dict:
        """Hot-reload every model in the hot set from MLflow."""
        try:
            self._load_hot_aliases()
            with self._cache_lock:
                self._version_cache.clear()
            self._reload_counter.inc(
                tags={"status": "success", "scope": "all", "replica": self._replica_id}
            )
        except Exception as exc:  # noqa: BLE001
            self._reload_counter.inc(
                tags={"status": "error", "scope": "all", "replica": self._replica_id}
            )
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        with self._cache_lock:
            hot_keys = list(self._hot.keys())
        return {
            "reloaded": sorted(f"{n}@{a}" for (n, a) in hot_keys),
            "count": len(hot_keys),
        }

    @_app.post("/reload/{model_name}", dependencies=[Depends(require_serving_admin)])
    def reload_model(self, model_name: str) -> dict:
        """Targeted hot-reload — used by the Prefect webhook on promotion."""
        try:
            count = self._reload_one_model(model_name)
            self._reload_counter.inc(
                tags={"status": "success", "scope": "single", "replica": self._replica_id}
            )
        except Exception as exc:  # noqa: BLE001
            self._reload_counter.inc(
                tags={"status": "error", "scope": "single", "replica": self._replica_id}
            )
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"model": model_name, "reloaded_aliases": count}


# ─── Entry point ─────────────────────────────────────────────────────────────


def _prepare_ray_environment() -> None:
    """Keep the platform's tracing switch from switching off Ray's metrics.

    ``OTEL_SDK_DISABLED=true`` is how the platform turns tracing off, and the default in Compose
    and Helm. Ray 2.55 records every metric (its own and ``ray.util.metrics`` ones such as
    ``ray_examlops_models_loaded``) through the OpenTelemetry SDK, which that same variable turns
    into a no-op: the metrics port then serves only process statistics, every serving alert and
    SLO panel is blind, and ``RayServeNoModelsLoaded`` fires permanently. The platform treats an
    unset variable as tracing off (``examlops.observability``), so removing a true value before
    Ray starts its processes keeps tracing off and gives Ray its metrics back.
    """
    if os.getenv("OTEL_SDK_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        del os.environ["OTEL_SDK_DISABLED"]


def main() -> None:
    _prepare_ray_environment()  # before ray.init: Ray's processes inherit this environment
    ray.init(
        ignore_reinit_error=True,
        include_dashboard=True,
        dashboard_host="0.0.0.0",
        _metrics_export_port=METRICS_EXPORT_PORT,
    )

    serve.start(http_options={"host": SERVE_HOST, "port": SERVE_PORT})
    serve.run(MultiModelServer.bind(), name="multi_model_server", route_prefix="/")  # type: ignore[attr-defined]

    from serving.inference_pipeline.app import pipeline_app  # noqa: PLC0415

    serve.run(pipeline_app, name="inference_pipeline", route_prefix="/infer-pipeline")

    grpc_server = None
    if GRPC_PORT > 0:
        # REST is the model server's primary interface: a gRPC front end that cannot start (a port
        # in use, a broken install) is logged loudly and REST keeps serving.
        try:
            from serving.oip_grpc.server import start_in_thread  # noqa: PLC0415

            grpc_server = start_in_thread(
                SERVE_HOST,
                GRPC_PORT,
                f"http://127.0.0.1:{SERVE_PORT}",
                max_message_bytes=GRPC_MAX_MESSAGE_BYTES,
            )
        except Exception:  # noqa: BLE001 - see above
            logger.exception(
                "OIP gRPC server did not start on port %d; REST is unaffected", GRPC_PORT
            )

    print("\nExaMLOps Ray Multi-Model Serving is up:")
    print(f"  API:       http://localhost:{SERVE_PORT}")
    print(f"  Models:    http://localhost:{SERVE_PORT}/models")
    print(f"  Pipeline:  http://localhost:{SERVE_PORT}/infer-pipeline/infer")
    print(f"  Docs:      http://localhost:{SERVE_PORT}/docs")
    if grpc_server is not None:
        print(f"  gRPC:      localhost:{grpc_server.port}  (inference.GRPCInferenceService)")
    print("  Dashboard: http://localhost:18265  (Serve → deployments)")
    print(f"  Replicas:  {NUM_REPLICAS} per model")
    print(f"  Aliases:   {PRELOAD_ALIASES}")
    print(f"  Polling:   every {RELOAD_POLL_SECONDS}s (0 = off)")
    print("\nPress Ctrl+C to stop.\n")

    stop = {"flag": False}

    def _handle_signal(sig, frame) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not stop["flag"]:
        time.sleep(1)

    print("Shutting down Ray Serve...")
    if grpc_server is not None:
        grpc_server.stop()
    serve.shutdown()
    ray.shutdown()


if __name__ == "__main__":
    main()
