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
    RAY_RELOAD_POLL_SECONDS     default: 60                    (0 disables polling)
    RAY_NUM_REPLICAS            default: 2
    RAY_SERVE_PORT              default: 8001
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

try:
    _REPO_ROOT_RS = _Path(__file__).resolve().parents[2]
    for _p in (
        str(_REPO_ROOT_RS),
        str(_REPO_ROOT_RS / "pipelines"),
        str(_REPO_ROOT_RS / "modelzoo"),
    ):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
except IndexError:
    pass

import asyncio
import logging
import os
import signal
import time
from collections import OrderedDict
from typing import Any

import mlflow
import mlflow.pyfunc
import ray
from fastapi import FastAPI, HTTPException
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
METRICS_EXPORT_PORT = int(os.getenv("RAY_METRICS_EXPORT_PORT", "8080"))

PRELOAD_ALIASES = [
    a.strip()
    for a in os.getenv("RAY_PRELOAD_ALIASES", "Production,Canary,Staging").split(",")
    if a.strip()
]
VERSION_CACHE_SIZE = int(os.getenv("RAY_VERSION_CACHE_SIZE", "8"))
RELOAD_POLL_SECONDS = int(os.getenv("RAY_RELOAD_POLL_SECONDS", "60"))

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

    def __init__(self, version: Any, tags: dict[str, str] | None = None) -> None:
        self.version = version
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


class PredictResponse(BaseModel):
    model_name: str
    alias: str | None
    model_version: str | None
    run_id: str | None
    prediction: Any


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


@serve.deployment(
    num_replicas=NUM_REPLICAS,
    ray_actor_options={"num_cpus": 1},
)
@serve.ingress(_app)
class MultiModelServer:
    def __init__(self) -> None:
        mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)

        # Pre-loaded hot set:
        #   key = (model_name, alias) — alias in PRELOAD_ALIASES
        #   value = {"model": pyfunc, "version": "3", "run_id": "..."}
        self._hot: dict[tuple[str, str], dict[str, Any]] = {}

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

        self._poll_task: asyncio.Task[None] | None = None

        import os as _os

        self._replica_id = _os.getenv("RAY_WORKER_ID", "default")

        # ── Online metrics ────────────────────────────────────────────────────
        self._req_counter = Counter(
            "examlops_predict_requests_total",
            description="Total prediction requests by model, version and outcome",
            tag_keys=("model_name", "version", "alias", "status"),
        )
        self._latency_hist = Histogram(
            "examlops_predict_latency_seconds",
            description="End-to-end prediction latency in seconds",
            boundaries=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
            tag_keys=("model_name", "version"),
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
        self._reload_counter = Counter(
            "examlops_reload_total",
            description="Number of hot-reload operations",
            tag_keys=("status", "scope", "replica"),
        )
        # ──────────────────────────────────────────────────────────────────────

        self._load_hot_aliases()
        self._start_poller()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _load_hot_aliases(self) -> None:
        """Scan MLflow for every (model, alias) in PRELOAD_ALIASES and load it."""
        client = mlflow.MlflowClient()
        try:
            registered = client.search_registered_models()
        except Exception as exc:  # noqa: BLE001
            logger.error("Cannot reach MLflow at %s: %s", MLFLOW_TRACKING_URI, exc)
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

        self._hot = new_hot
        self._models_gauge.set(len(self._hot), tags={"replica": self._replica_id})
        logger.info(
            "Hot set ready — %d entries: %s",
            len(self._hot),
            sorted(f"{n}@{a}" for (n, a) in self._hot),
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
        uri = f"models:/{name}{suffix}"

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
            except Exception:  # noqa: BLE001
                self._hot.pop((model_name, alias), None)
                continue
            try:
                model = self._load_by_flavour(model_name, alias, mv)
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
        for k in [k for k in self._version_cache if k[0] == model_name]:
            self._version_cache.pop(k, None)
        self._models_gauge.set(len(self._hot), tags={"replica": self._replica_id})
        return reloaded

    def _start_poller(self) -> None:
        """Launch the background MLflow poller (no-op when RELOAD_POLL_SECONDS=0)."""
        if RELOAD_POLL_SECONDS <= 0:
            logger.info("RAY_RELOAD_POLL_SECONDS=0 — polling disabled.")
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        self._poll_task = loop.create_task(self._poll_loop())
        logger.info("MLflow alias poller started (interval=%ss)", RELOAD_POLL_SECONDS)

    async def _poll_loop(self) -> None:
        """Refresh any (model, alias) entry whose MLflow version changed."""
        client = mlflow.MlflowClient()
        while True:
            try:
                await asyncio.sleep(RELOAD_POLL_SECONDS)
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
        for key, hot_entry in self._hot.items():
            if observed.get(key) != hot_entry["version"]:
                changed.add(key[0])
        # Brand-new (model, alias) pairs that we haven't loaded yet.
        for key, version in observed.items():
            if version is not None and key not in self._hot:
                changed.add(key[0])
        return changed

    def _resolve(self, model_name: str, alias: str | None, version: str | None) -> dict[str, Any]:
        """Return ``{"model", "version", "alias", "run_id"}`` for the request.

        Raises HTTPException(404) when the requested combo can't be loaded.
        """
        # 1. Alias takes precedence over raw version.
        if alias:
            entry = self._hot.get((model_name, alias))
            if entry is not None:
                return {**entry, "alias": alias}
            # Alias not in the hot set — try a one-shot load.
            try:
                client = mlflow.MlflowClient()
                mv = client.get_model_version_by_alias(model_name, alias)
                model = self._load_by_flavour(model_name, alias, mv)
                entry = {
                    "model": model,
                    "version": str(mv.version),
                    "run_id": getattr(getattr(model, "metadata", None), "run_id", None),
                }
                self._hot[(model_name, alias)] = entry
                return {**entry, "alias": alias}
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=404,
                    detail=f"Alias '{alias}' not found for model '{model_name}': {exc}",
                ) from exc

        # 2. Raw version — LRU cache.
        if version:
            key = (model_name, str(version))
            entry = self._version_cache.get(key)
            if entry is not None:
                self._version_cache.move_to_end(key)
                return {**entry, "alias": None}
            try:
                client = mlflow.MlflowClient()
                try:
                    mv = client.get_model_version(model_name, str(version))
                except Exception:  # noqa: BLE001
                    mv = None
                model = self._load_by_flavour(model_name, None, mv or _StubMV(version))
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=404,
                    detail=f"Version '{version}' not found for model '{model_name}': {exc}",
                ) from exc
            entry = {
                "model": model,
                "version": str(version),
                "run_id": getattr(getattr(model, "metadata", None), "run_id", None),
            }
            self._version_cache[key] = entry
            self._version_cache.move_to_end(key)
            while len(self._version_cache) > self._version_cache_size:
                self._version_cache.popitem(last=False)
            return {**entry, "alias": None}

        # 3. Default alias (MODEL_STAGE).
        entry = self._hot.get((model_name, MODEL_STAGE))
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Model '{model_name}' has no '{MODEL_STAGE}' alias loaded. "
                    f"Available: {sorted(set(n for (n, _) in self._hot))}"
                ),
            )
        return {**entry, "alias": MODEL_STAGE}

    # ── Routes ────────────────────────────────────────────────────────────────

    @_app.get("/health")
    def health(self) -> dict:
        """Liveness + readiness — reports every (model, alias) currently held."""
        return {
            "status": "ok" if self._hot else "degraded",
            "models_loaded": len(self._hot),
            "version_cache_size": len(self._version_cache),
            "models": [
                {
                    "model_name": name,
                    "alias": alias,
                    "version": entry["version"],
                    "run_id": entry["run_id"],
                    "status": "ok",
                }
                for (name, alias), entry in self._hot.items()
            ],
        }

    @_app.get("/models", response_model=list[ModelInfo])
    def list_models(self) -> list[ModelInfo]:
        """Every (model, alias) pair currently in the hot set."""
        return [
            ModelInfo(
                model_name=name,
                alias=alias,
                model_version=entry["version"],
                run_id=entry["run_id"],
                status="ok",
            )
            for (name, alias), entry in self._hot.items()
        ]

    @_app.post("/predict/{model_name}", response_model=PredictResponse)
    def predict(self, model_name: str, request: PredictRequest) -> PredictResponse:
        """Run inference for *model_name* with optional alias / version selection."""
        try:
            resolved = self._resolve(model_name, request.alias, request.version)
        except HTTPException:
            self._req_counter.inc(
                tags={
                    "model_name": model_name,
                    "version": request.version or "",
                    "alias": request.alias or "",
                    "status": "not_found",
                }
            )
            raise

        model = resolved["model"]
        version = resolved["version"]
        alias = resolved["alias"]

        _t0 = time.time()
        try:
            import numpy as np

            row: list = []
            for v in request.features.values():
                if isinstance(v, list):
                    row.extend(v)
                else:
                    row.append(v)
            input_array = np.array([row], dtype=float)
            raw = model.predict(input_array)
            prediction: Any = raw.tolist() if hasattr(raw, "tolist") else raw
            if isinstance(prediction, list) and len(prediction) == 1:
                prediction = prediction[0]
        except Exception as exc:  # noqa: BLE001
            self._req_counter.inc(
                tags={
                    "model_name": model_name,
                    "version": version or "",
                    "alias": alias or "",
                    "status": "error",
                }
            )
            self._latency_hist.observe(
                time.time() - _t0,
                tags={"model_name": model_name, "version": version or ""},
            )
            logger.error("Prediction error for '%s' (v%s): %s", model_name, version, exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        _latency = time.time() - _t0
        self._req_counter.inc(
            tags={
                "model_name": model_name,
                "version": version or "",
                "alias": alias or "",
                "status": "success",
            }
        )
        self._latency_hist.observe(
            _latency, tags={"model_name": model_name, "version": version or ""}
        )
        if isinstance(prediction, (int, float)):
            self._pred_value_hist.observe(float(prediction), tags={"model_name": model_name})

        logger.info(
            "predict | model=%s alias=%s v%s → %s (%.3fs)",
            model_name,
            alias,
            version,
            prediction,
            _latency,
        )
        return PredictResponse(
            model_name=model_name,
            alias=alias,
            model_version=version,
            run_id=resolved["run_id"],
            prediction=prediction,
        )

    @_app.post("/reload")
    def reload(self) -> dict:
        """Hot-reload every model in the hot set from MLflow."""
        try:
            self._load_hot_aliases()
            self._version_cache.clear()
            self._reload_counter.inc(
                tags={"status": "success", "scope": "all", "replica": self._replica_id}
            )
        except Exception as exc:  # noqa: BLE001
            self._reload_counter.inc(
                tags={"status": "error", "scope": "all", "replica": self._replica_id}
            )
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "reloaded": sorted(f"{n}@{a}" for (n, a) in self._hot),
            "count": len(self._hot),
        }

    @_app.post("/reload/{model_name}")
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


def main() -> None:
    ray.init(
        ignore_reinit_error=True,
        include_dashboard=True,
        dashboard_host="0.0.0.0",
        _metrics_export_port=METRICS_EXPORT_PORT,
    )

    serve.start(http_options={"host": "0.0.0.0", "port": SERVE_PORT})
    serve.run(MultiModelServer.bind(), name="multi_model_server", route_prefix="/")  # type: ignore[attr-defined]

    from serving.inference_pipeline.app import pipeline_app  # noqa: PLC0415

    serve.run(pipeline_app, name="inference_pipeline", route_prefix="/infer-pipeline")

    print("\nExaMLOps Ray Multi-Model Serving is up:")
    print(f"  API:       http://localhost:{SERVE_PORT}")
    print(f"  Models:    http://localhost:{SERVE_PORT}/models")
    print(f"  Pipeline:  http://localhost:{SERVE_PORT}/infer-pipeline/infer")
    print(f"  Docs:      http://localhost:{SERVE_PORT}/docs")
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
    serve.shutdown()
    ray.shutdown()


if __name__ == "__main__":
    main()
