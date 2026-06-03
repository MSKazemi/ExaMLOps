"""
Ray Serve Multi-Model Inference Service – ExaMLOps.

Discovers ALL Production-stage models from MLflow at startup and serves
them through a single Ray Serve deployment. Models can be hot-reloaded
via POST /reload without restarting the process.

Routes (served on RAY_SERVE_PORT, default 8001):
    GET  /health                  liveness + per-model status
    GET  /models                  list all loaded models with metadata
    POST /predict/{model_name}    generic prediction (dict of features)
    POST /reload                  re-scan MLflow and hot-reload all models
    GET  /docs                    FastAPI Swagger UI
    GET  /redoc                   FastAPI ReDoc UI

Ray dashboard: http://localhost:8265  (Serve → deployments, metrics, logs)

Start:
    make ray-serving-start
    # or directly:
    python app.py

Env vars:
    MLFLOW_TRACKING_URI   default: http://localhost:5000
    MODEL_STAGE           default: Production
    RAY_NUM_REPLICAS      default: 2
    RAY_SERVE_PORT        default: 8001
"""

import logging
import os
import signal
import time
from typing import Any

import mlflow
import mlflow.pyfunc
import pandas as pd
import ray
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from ray import serve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ray_serving")

# ─── Config ──────────────────────────────────────────────────────────────────

MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
MODEL_STAGE = os.getenv("MODEL_STAGE", "Production")
NUM_REPLICAS = int(os.getenv("RAY_NUM_REPLICAS", "2"))
SERVE_PORT = int(os.getenv("RAY_SERVE_PORT", "8001"))

# ─── Schemas ─────────────────────────────────────────────────────────────────


class PredictRequest(BaseModel):
    features: dict[str, Any]


class ModelInfo(BaseModel):
    model_name: str
    model_version: str | None
    run_id: str | None
    status: str


class PredictResponse(BaseModel):
    model_name: str
    model_version: str | None
    run_id: str | None
    prediction: Any


# ─── Ray Serve deployment ─────────────────────────────────────────────────────
#
# @serve.ingress(app) wires FastAPI directly into Ray's HTTP layer.
# Each replica gets its own __init__ — models are loaded independently
# per replica (no shared state, no locking).
#
# The multi-model design:
#   - On init, scans MLflow for ALL registered models that have a
#     Production version and loads each one.
#   - POST /predict/{model_name} routes to the right loaded model.
#   - POST /reload hot-reloads all models from MLflow without restart.

_app = FastAPI(
    title="ExaMLOps Ray Multi-Model Serving",
    version="0.2.0",
    description=(
        "All Production-stage models from the MLflow registry — "
        "served via Ray Serve with autoscaling and a built-in dashboard."
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
        self._models: dict[str, mlflow.pyfunc.PyFuncModel] = {}
        self._meta: dict[str, dict[str, str | None]] = {}
        self._load_all_models()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _load_all_models(self) -> None:
        """Scan MLflow registry and load every Production model."""
        client = mlflow.MlflowClient()
        try:
            registered = client.search_registered_models()
        except Exception as exc:
            logger.error("Cannot reach MLflow at %s: %s", MLFLOW_TRACKING_URI, exc)
            return

        loaded_count = 0
        for rm in registered:
            name = rm.name
            try:
                mv = client.get_model_version_by_alias(name, MODEL_STAGE)
            except Exception:
                logger.debug("No '%s' alias for model '%s' — skipping", MODEL_STAGE, name)
                continue
            try:
                uri = f"models:/{name}@{MODEL_STAGE}"
                model = mlflow.pyfunc.load_model(uri)
                run_id = model.metadata.run_id
                self._models[name] = model
                self._meta[name] = {"version": str(mv.version), "run_id": run_id}
                loaded_count += 1
                logger.info("Loaded '%s' (v%s, run_id=%s)", name, mv.version, run_id)
            except Exception as exc:
                logger.error("Failed to load '%s': %s", name, exc)

        logger.info(
            "Startup complete — %d model(s) loaded: %s",
            loaded_count,
            list(self._models),
        )

    # ── Routes ────────────────────────────────────────────────────────────────

    @_app.get("/health")
    def health(self) -> dict:
        """Liveness + readiness — reports status of every loaded model."""
        return {
            "status": "ok" if self._models else "degraded",
            "models_loaded": len(self._models),
            "models": {
                name: {
                    "version": m["version"],
                    "run_id": m["run_id"],
                    "status": "ok",
                }
                for name, m in self._meta.items()
            },
        }

    @_app.get("/models", response_model=list[ModelInfo])
    def list_models(self) -> list[ModelInfo]:
        """List all loaded models with their MLflow version and run ID."""
        return [
            ModelInfo(
                model_name=name,
                model_version=m["version"],
                run_id=m["run_id"],
                status="ok",
            )
            for name, m in self._meta.items()
        ]

    @_app.post("/predict/{model_name}", response_model=PredictResponse)
    def predict(self, model_name: str, request: PredictRequest) -> PredictResponse:
        """Run inference for *model_name* using the provided feature dict.

        The features dict must match the column names the model was trained on.
        """
        model = self._models.get(model_name)
        if model is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Model '{model_name}' is not loaded or has no {MODEL_STAGE} version. "
                    f"Available: {list(self._models)}"
                ),
            )
        try:
            import numpy as np
            # Flatten list-valued features (e.g. embedding vectors) into a single
            # numeric row so sklearn receives a proper (1, n_features) array.
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
        except Exception as exc:
            logger.error("Prediction error for '%s': %s", model_name, exc)
            raise HTTPException(status_code=500, detail=str(exc))

        meta = self._meta[model_name]
        logger.info(
            "predict | model=%s → %s (v%s)",
            model_name,
            prediction,
            meta["version"],
        )
        return PredictResponse(
            model_name=model_name,
            model_version=meta["version"],
            run_id=meta["run_id"],
            prediction=prediction,
        )

    @_app.post("/reload")
    def reload(self) -> dict:
        """Hot-reload: re-scan MLflow and refresh all Production models.

        Call this after promoting a new model version to Production —
        no process restart needed.
        """
        self._models.clear()
        self._meta.clear()
        self._load_all_models()
        return {"reloaded": list(self._models), "count": len(self._models)}


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    ray.init(
        ignore_reinit_error=True,
        include_dashboard=True,
        dashboard_host="0.0.0.0",
    )

    serve.start(http_options={"host": "0.0.0.0", "port": SERVE_PORT})
    serve.run(MultiModelServer.bind(), name="multi_model_server", route_prefix="/")

    print("\nExaMLOps Ray Multi-Model Serving is up:")
    print(f"  API:       http://localhost:{SERVE_PORT}")
    print(f"  Models:    http://localhost:{SERVE_PORT}/models")
    print(f"  Docs:      http://localhost:{SERVE_PORT}/docs")
    print("  Dashboard: http://localhost:8265  (Serve → deployments)")
    print(f"  Replicas:  {NUM_REPLICAS} per model")
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
