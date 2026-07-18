# serving/inference_pipeline/app.py
import asyncio
import logging
import os
import random
import threading
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from opentelemetry import trace as _otel_trace
from ray import serve

from examlops.observability import setup_tracing

try:
    from examlops.platform_db import get_traffic_rules as _db_get_traffic
    from examlops.platform_db import set_traffic_rules as _db_set_traffic
    from examlops.resilience import httpx_timeout as _httpx_timeout
except Exception:  # pragma: no cover - examlops always present in the serving image
    _db_get_traffic = _db_set_traffic = None  # type: ignore[assignment]

    def _httpx_timeout(read=None, connect=None):  # type: ignore[misc]
        return httpx.Timeout(10.0)


_log = logging.getLogger("inference_pipeline")

_DEFAULT_MODEL = os.getenv("SEANERBUS_DEFAULT_MODEL", "JPCP")
_DEFAULT_ALIAS = os.getenv("SEANERBUS_DEFAULT_ALIAS", "Production")
_RAY_SERVE_URL = os.getenv("RAY_SERVE_URL", "http://localhost:8001").rstrip("/")
# Transient-error retries for the ingress→MultiModelServer hop.
_ROUTE_RETRIES = int(os.getenv("INFERENCE_ROUTE_RETRIES", "2"))

_traffic_rules: dict[str, dict[str, int]] = {}
_traffic_lock = threading.Lock()


def _get_split(model_name: str) -> dict[str, int] | None:
    """Return the traffic split for *model_name*.

    In-memory per-replica cache first; on a miss, fall back to the durable
    ``platform_db`` copy so rules survive replica restarts and stay consistent
    across replicas (and with the ``exa serve traffic`` CLI). DB errors degrade
    to "no split" rather than failing inference.
    """
    with _traffic_lock:
        cached = _traffic_rules.get(model_name)
    if cached is not None:
        return cached
    if _db_get_traffic is None:
        return None
    try:
        rules = _db_get_traffic(model_name)
    except Exception as exc:  # noqa: BLE001
        _log.warning("traffic-rule DB read failed for %s: %s", model_name, exc)
        return None
    if rules:
        with _traffic_lock:
            _traffic_rules[model_name] = rules
    return rules or None


# No-op unless OTEL_SDK_DISABLED=false; get_tracer returns a no-op tracer otherwise.
setup_tracing("ray-serving")
_tracer = _otel_trace.get_tracer("examlops.inference_pipeline")

_ingress_app = FastAPI()


class ModelRouter:
    @staticmethod
    def _resolve(payload: dict[str, Any]) -> tuple[str, str]:
        # MLflow stores model names as lowercase; normalise here so callers can
        # pass "JPCP" (the canonical YAML name) or "jpcp" interchangeably.
        model_name = (payload.get("model_name") or _DEFAULT_MODEL).lower()
        requested_alias = payload.get("alias") or _DEFAULT_ALIAS
        split = _get_split(model_name)
        if split and len(split) > 1:
            aliases = list(split.keys())
            weights = [split[a] for a in aliases]
            chosen = random.choices(aliases, weights=weights, k=1)[0]
            return model_name, chosen
        return model_name, requested_alias

    async def route(self, payload: dict[str, Any]) -> dict[str, Any]:
        model_name, alias = self._resolve(payload)
        features = payload["features"]
        with _tracer.start_as_current_span("inference_pipeline.model_router") as span:
            span.set_attribute("model_name", model_name)
            span.set_attribute("alias", alias)
            last_exc: Exception | None = None
            # Retry transient transport errors (a single Ray Serve blip shouldn't
            # surface as inference_failed); HTTP status errors are definitive.
            for attempt in range(_ROUTE_RETRIES + 1):
                try:
                    async with httpx.AsyncClient(timeout=_httpx_timeout()) as client:
                        resp = await client.post(
                            f"{_RAY_SERVE_URL}/predict/{model_name}",
                            json={"features": features, "alias": alias},
                        )
                    if resp.status_code == 404:
                        return {
                            "error": "model_not_found",
                            "model_name": model_name,
                            "alias": alias,
                        }
                    resp.raise_for_status()
                    return resp.json()
                except httpx.HTTPStatusError as exc:
                    return {"error": "inference_failed", "detail": str(exc)}
                except httpx.RequestError as exc:
                    last_exc = exc
                    if attempt < _ROUTE_RETRIES:
                        await asyncio.sleep(0.1 * (2**attempt))
                except Exception as exc:  # noqa: BLE001
                    return {"error": "inference_failed", "detail": str(exc)}
            return {"error": "inference_failed", "detail": str(last_exc)}


class FeatureTransformer:
    def __init__(self, router: Any) -> None:
        self._router = router

    @staticmethod
    def _transform_one(req: dict[str, Any]) -> dict[str, Any]:
        embedding = req.get("embedding")
        if embedding is None:
            raise ValueError("embedding is required")
        if len(embedding) != 384:
            raise ValueError(f"expected 384 dims, got {len(embedding)}")
        num_nodes = req.get("num_nodes")
        if num_nodes is None:
            raise ValueError("num_nodes is required")
        # The deployed models are FData-trained and consume the embedding only.
        # num_nodes / user_id are carried as job metadata, not model features —
        # adding them to the feature dict produces a wrong-shaped input array.
        return {
            "model_name": req.get("model_name"),
            "alias": req.get("alias"),
            "job_id": req.get("job_id"),
            "num_nodes": int(num_nodes),
            "user_id": str(req.get("user_id", "")),
            "features": {
                "embedding": list(embedding),
            },
        }

    @serve.batch(max_batch_size=32, batch_wait_timeout_s=0.05)
    async def handle_batch(self, reqs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any] | None] = [None] * len(reqs)
        valid: list[tuple[int, dict[str, Any]]] = []

        for i, req in enumerate(reqs):
            try:
                transformed = self._transform_one(req)
                valid.append((i, transformed))
            except ValueError as exc:
                results[i] = {"error": "validation_error", "detail": str(exc)}

        if valid:
            router_results = await asyncio.gather(
                *[self._router.route.remote(payload) for _, payload in valid],
                return_exceptions=True,
            )
            for (idx, _), result in zip(valid, router_results):
                if isinstance(result, BaseException):
                    results[idx] = {"error": "inference_failed", "detail": str(result)}
                else:
                    results[idx] = result

        return results  # type: ignore[return-value]


class InferencePipelineIngress:
    def __init__(self, transformer: Any) -> None:
        self._transformer = transformer

    @_ingress_app.get("/health")
    async def health(self) -> dict[str, Any]:
        """Liveness for the inference-pipeline ingress (process + loop responsive)."""
        return {"status": "alive", "ray_serve_url": _RAY_SERVE_URL}

    @_ingress_app.post("/traffic-rules/{model}", response_model=None)
    async def set_traffic(self, model: str, body: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        try:
            rules = {k: int(v) for k, v in body.items()}
        except (TypeError, ValueError) as exc:
            return JSONResponse(
                {"error": "validation_error", "detail": f"rule values must be integers: {exc}"},
                status_code=422,
            )
        with _traffic_lock:
            _traffic_rules[model.lower()] = rules
        # Persist so the split survives replica restarts and is shared across replicas.
        if _db_set_traffic is not None:
            try:
                _db_set_traffic(model.lower(), rules, updated_by="inference_pipeline")
            except Exception as exc:  # noqa: BLE001
                _log.warning("traffic-rule DB write failed for %s: %s", model, exc)
        return {"ok": True, "model": model, "rules": rules}

    @_ingress_app.get("/traffic-rules")
    async def get_traffic(self) -> dict[str, Any]:
        with _traffic_lock:
            return dict(_traffic_rules)

    @_ingress_app.post("/infer")
    async def infer(self, body: dict[str, Any]) -> Any:
        with _tracer.start_as_current_span("inference_pipeline.ingress") as span:
            span.set_attribute("model_name", body.get("model_name") or "")
            span.set_attribute("alias", body.get("alias") or "")
            for field in ("embedding", "num_nodes"):
                if field not in body:
                    return JSONResponse(
                        {"error": "validation_error", "detail": f"{field} is required"},
                        status_code=422,
                    )
            result = await self._transformer.handle_batch.remote(body)
            if "error" not in result:
                return result
            err = result["error"]
            if err == "validation_error":
                return JSONResponse(result, status_code=422)
            if err == "model_not_found":
                return JSONResponse(result, status_code=404)
            return JSONResponse(result, status_code=500)


# Apply Ray Serve decorators after class definitions so the plain class names
# remain accessible for unit testing (static methods, etc.).
#
# num_cpus=0: these three deployments are I/O-bound orchestrators — they only
# `await` (Ray-handle calls + an httpx POST to /predict), never compute — so
# reserving a full CPU each is wrong and, on a CPU-capped node, starves them:
# multi_model_server (num_cpus=1) + ModelRouter/FeatureTransformer/Ingress at 1
# CPU each = 4+ CPUs, which cannot fit the default 2-CPU container limit, leaving
# FeatureTransformer + Ingress permanently UPDATING and /infer-pipeline hanging.
# num_cpus=0 is Ray Serve's recommended setting for lightweight routing
# deployments and makes the topology fit any node. Only the compute-bound
# model server keeps its dedicated core.
_PIPELINE_ACTOR_OPTS = {"num_cpus": 0}
_ModelRouterDeployment = serve.deployment(num_replicas=1, ray_actor_options=_PIPELINE_ACTOR_OPTS)(
    ModelRouter
)
_FeatureTransformerDeployment = serve.deployment(
    num_replicas=1, ray_actor_options=_PIPELINE_ACTOR_OPTS
)(FeatureTransformer)
_IngressDeployment = serve.deployment(num_replicas=1, ray_actor_options=_PIPELINE_ACTOR_OPTS)(
    serve.ingress(_ingress_app)(InferencePipelineIngress)
)

# Deployment graph — imported by serving/ray_serving/app.py
router = _ModelRouterDeployment.bind()
transformer = _FeatureTransformerDeployment.bind(router)
pipeline_app = _IngressDeployment.bind(transformer)
