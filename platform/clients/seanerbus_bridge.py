"""
SeanerBUS → ExaMLOps Ray Serve bridge.

Connects a Cap'n Proto / TCP seanerbus server to ExaMLOps's Ray Serve inference
API. Supports three modes via SEANERBUS_MODE:

  pubsub   — subscribe to HpcJobV1 messages, call Ray Serve, optionally publish
             HpcInferenceResV1 results back to a result topic
  reqres   — register two handlers: inference (HpcJobV1 → HpcInferenceResV1)
             and retrain trigger (RetrainReqV1 → RetrainResV1)
  both     — run all of the above concurrently, each on its own Connection

Env vars (defaults shown):
    SEANERBUS_HOST=localhost
    SEANERBUS_PORT=5398
    SEANERBUS_MODE=both
    SEANERBUS_JOB_TOPIC_UUID        required for pubsub / both
    SEANERBUS_RESULT_TOPIC_UUID     optional; enables result publishing
    SEANERBUS_INFERENCE_UUID        required for reqres / both
    SEANERBUS_RETRAIN_UUID          required for reqres / both
    SEANERBUS_VECTOR_UUID           required for vector req/res; omit to disable
    RAY_SERVE_URL=http://localhost:8001
    CONTROL_PLANE_URL=http://localhost:8002
    CONTROL_PLANE_TOKEN=
    SEANERBUS_DEFAULT_MODEL=JPCP
    SEANERBUS_DEFAULT_ALIAS=Production
    SEANERBUS_PUBLISH_RESULTS=true
    DRIFT_WINDOW=50
    DRIFT_THRESHOLD=0.5
    DRIFT_COOLDOWN=300
    MODELS_YAML_DIR=<repo>/pipelines/models
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from asyncio import StreamReader, StreamWriter
from typing import Any

import capnp
import httpx
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# Allow importing sibling packages when run as `python clients/seanerbus_bridge.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_CLI_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cli", "src")
sys.path.insert(0, os.path.abspath(_CLI_SRC))

try:
    from examlops.platform_db import (
        init_db as _init_platform_db,
        write_audit_event,
        write_drift_snapshot,
        write_input_snapshot,
    )
    _init_platform_db()
    _PLATFORM_DB_AVAILABLE = True
except Exception:
    _PLATFORM_DB_AVAILABLE = False
    def write_drift_snapshot(*a, **k): pass  # type: ignore[misc]
    def write_audit_event(*a, **k): pass  # type: ignore[misc]
    def write_input_snapshot(*a, **k): pass  # type: ignore[misc]

from model_schema_registry import ModelSchemaRegistry  # noqa: E402
from seanerbus_client import Connection  # noqa: E402
from seanerbus_msgs import (  # noqa: E402
    HpcInferenceResV1,
    HpcJobV1,
    RetrainReqV1,
    RetrainResV1,
    VectorReqV1,
    VectorResV1,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [seanerbus-bridge] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("seanerbus_bridge")
logging.getLogger("httpx").setLevel(logging.WARNING)  # suppress per-request HTTP noise

# ── configuration ──────────────────────────────────────────────────────────────

SEANERBUS_HOST = os.getenv("SEANERBUS_HOST", "localhost")
SEANERBUS_PORT = int(os.getenv("SEANERBUS_PORT", "5398"))
SEANERBUS_MODE = os.getenv("SEANERBUS_MODE", "both").lower()

RAY_SERVE_URL = os.getenv("RAY_SERVE_URL", "http://localhost:8001").rstrip("/")
CONTROL_PLANE_URL = os.getenv("CONTROL_PLANE_URL", "http://localhost:8002").rstrip("/")
CONTROL_PLANE_TOKEN = os.getenv("CONTROL_PLANE_TOKEN", "")

DEFAULT_MODEL = os.getenv("SEANERBUS_DEFAULT_MODEL", "JPCP")
DEFAULT_ALIAS = os.getenv("SEANERBUS_DEFAULT_ALIAS", "Production")
PUBLISH_RESULTS = os.getenv("SEANERBUS_PUBLISH_RESULTS", "true").lower() == "true"

DRIFT_WINDOW = int(os.getenv("DRIFT_WINDOW", os.getenv("CLIENT_SIM_DRIFT_WINDOW", "50")))
DRIFT_THRESHOLD = float(os.getenv("DRIFT_THRESHOLD", os.getenv("CLIENT_SIM_DRIFT_THRESHOLD", "0.5")))
DRIFT_COOLDOWN = int(os.getenv("DRIFT_COOLDOWN", os.getenv("CLIENT_SIM_DRIFT_COOLDOWN", "300")))
MODELS_YAML_DIR = os.getenv("MODELS_YAML_DIR", "")

_schema_registry = ModelSchemaRegistry(MODELS_YAML_DIR if MODELS_YAML_DIR else None)


def _parse_uuid(env_var: str) -> uuid.UUID | None:
    raw = os.getenv(env_var, "")
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        return None


JOB_TOPIC_UUID = _parse_uuid("SEANERBUS_JOB_TOPIC_UUID")
RESULT_TOPIC_UUID = _parse_uuid("SEANERBUS_RESULT_TOPIC_UUID")
INFERENCE_UUID = _parse_uuid("SEANERBUS_INFERENCE_UUID")
RETRAIN_UUID = _parse_uuid("SEANERBUS_RETRAIN_UUID")
VECTOR_UUID = _parse_uuid("SEANERBUS_VECTOR_UUID")


def _load_model_uuids() -> dict[str, uuid.UUID]:
    """Read seanerbus_uuid from every model YAML in MODELS_YAML_DIR.

    Returns {UPPERCASE_MODEL_NAME: uuid.UUID}. Models without seanerbus_uuid
    are silently skipped. Errors in individual files are logged and skipped.
    """
    if not MODELS_YAML_DIR:
        return {}
    from pathlib import Path

    import yaml as _yaml
    models_path = Path(MODELS_YAML_DIR)
    if not models_path.is_dir():
        log.warning("MODELS_YAML_DIR=%r is not a directory — per-model UUIDs disabled", MODELS_YAML_DIR)
        return {}
    result: dict[str, uuid.UUID] = {}
    for yaml_file in sorted(models_path.glob("*.yaml")):
        if yaml_file.stem.startswith("_"):
            continue
        try:
            raw = _yaml.safe_load(yaml_file.read_text()) or {}
        except Exception as exc:
            log.warning("Could not parse %s: %s", yaml_file.name, exc)
            continue
        raw_uuid = raw.get("seanerbus_uuid", "")
        model_name = raw.get("name", "")
        if raw_uuid and model_name:
            try:
                result[model_name.upper()] = uuid.UUID(raw_uuid)
            except ValueError:
                log.warning("Invalid seanerbus_uuid in %s — skipping", yaml_file.name)
    return result


MODEL_UUIDS: dict[str, uuid.UUID] = _load_model_uuids()

# ── Prometheus metrics ─────────────────────────────────────────────────────────

_BRIDGE_UP = Gauge(
    "seanerbus_bridge_up",
    "Set to 1.0 while the bridge process is running",
)
_INFERENCES = Counter(
    "seanerbus_inferences_total",
    "Total inference calls dispatched to Ray Serve",
    ["model"],
)
_ERRORS = Counter(
    "seanerbus_inference_errors_total",
    "Total inference calls that returned an error",
    ["model"],
)
_LATENCY = Histogram(
    "seanerbus_inference_latency_seconds",
    "End-to-end inference latency from bridge POST to Ray Serve response",
    ["model"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)
_RETRAINS = Counter(
    "seanerbus_retrain_triggers_total",
    "Total drift-triggered retrains posted to the Control Plane",
)

# ── shared stats ───────────────────────────────────────────────────────────────

# Shared inference stats — updated by handlers, read by HTTP status server
_bridge_stats: dict[str, Any] = {
    "mode": "",
    "inferences_total": 0,
    "retrains_total": 0,
    "vectors_total": 0,
    "per_model": {},
}


async def _handle_status_request(reader: StreamReader, writer: StreamWriter) -> None:
    """Minimal HTTP/1.1 handler for GET /health and GET /stats."""
    try:
        raw = await asyncio.wait_for(reader.read(4096), timeout=2.0)
        first_line = raw.split(b"\r\n")[0].decode(errors="replace")
        parts = first_line.split(" ")
        path = parts[1] if len(parts) >= 2 else "/"

        if path == "/health":
            body = json.dumps({"status": "ok", "mode": _bridge_stats["mode"]})
            status_line = "HTTP/1.1 200 OK"
        elif path == "/stats":
            body = json.dumps(_bridge_stats)
            status_line = "HTTP/1.1 200 OK"
        elif path == "/metrics":
            raw_body = generate_latest()
            response = (
                "HTTP/1.1 200 OK\r\n"
                f"Content-Type: {CONTENT_TYPE_LATEST}\r\n"
                f"Content-Length: {len(raw_body)}\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                "\r\n"
            )
            writer.write(response.encode() + raw_body)
            await writer.drain()
            return
        else:
            body = json.dumps({"error": "not found"})
            status_line = "HTTP/1.1 404 Not Found"

        response = (
            f"{status_line}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "\r\n"
        )
        writer.write(response.encode() + body.encode())
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def run_status_server(port: int = 8003) -> None:
    """Start a tiny asyncio HTTP server on :8003 for /health and /stats."""
    server = await asyncio.start_server(_handle_status_request, "0.0.0.0", port)
    log.info("Bridge status server listening on :%d", port)
    async with server:
        await server.serve_forever()


# ── drift tracker ──────────────────────────────────────────────────────────────

class DriftTracker:
    """Per-model rolling-error tracker; auto-triggers control-plane retrain on drift."""

    def __init__(
        self,
        window: int = DRIFT_WINDOW,
        threshold: float = DRIFT_THRESHOLD,
        cooldown: int = DRIFT_COOLDOWN,
    ) -> None:
        self.window = window
        self.threshold = threshold
        self.cooldown = cooldown
        self._results: dict[str, list[bool]] = {}
        self._last_retrain: dict[str, float] = {}

    def record(self, model: str, success: bool) -> None:
        bucket = self._results.setdefault(model, [])
        bucket.append(success)
        if len(bucket) > self.window:
            del bucket[0]
        if len(bucket) >= self.window and self._error_rate(bucket) >= self.threshold:
            asyncio.create_task(self._maybe_trigger(model, self._error_rate(bucket)))

    def _error_rate(self, bucket: list[bool]) -> float:
        return sum(1 for s in bucket if not s) / len(bucket)

    async def _maybe_trigger(self, model: str, rate: float) -> None:
        now = time.time()
        if now - self._last_retrain.get(model, 0) < self.cooldown:
            return
        self._last_retrain[model] = now
        log.warning("Drift detected for %s (error_rate=%.0f%%) — triggering retrain", model, rate * 100)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if CONTROL_PLANE_TOKEN:
            headers["Authorization"] = f"Bearer {CONTROL_PLANE_TOKEN}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{CONTROL_PLANE_URL}/retrain",
                    json={
                        "model_name": model,
                        "dataset_name": "FDataDataset",
                        "backend_name": "dataplane",
                        "is_dummy": False,
                    },
                    headers=headers,
                )
            log.info("Retrain triggered for %s → HTTP %d", model, r.status_code)
            _RETRAINS.inc()
            _bridge_stats["retrains_total"] += 1
        except httpx.HTTPError as exc:
            log.error("Retrain trigger failed for %s: %s", model, exc)


_drift_tracker = DriftTracker()


# ── inference pipeline call ────────────────────────────────────────────────────

async def _call_pipeline(job: HpcJobV1, override_model: str | None = None) -> tuple[float | None, str | None, str | None]:
    """POST HpcJobV1 fields to the inference pipeline; return (prediction, run_id, version)."""
    model_name = override_model or job.model_name or DEFAULT_MODEL
    alias = job.alias or DEFAULT_ALIAS
    features = _schema_registry.build_features(model_name, job)
    _schema_registry.validate_features(model_name, features)

    log.info(
        "← REQ  job=%-8s  model=%s  alias=%s  nodes=%d  user=%s",
        job.job_id[:8],
        model_name,
        alias,
        job.num_nodes,
        job.user_id,
    )

    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{RAY_SERVE_URL}/infer-pipeline/infer",
            json={
                **features,  # spread first so fixed metadata fields always win
                "job_id": str(job.job_id),
                "model_name": model_name,
                "alias": alias,
                "num_nodes": job.num_nodes,
                "user_id": str(job.user_id),
            },
        )
        resp.raise_for_status()
    latency_s = time.perf_counter() - t0
    _INFERENCES.labels(model=model_name).inc()
    _LATENCY.labels(model=model_name).observe(latency_s)
    latency_ms = latency_s * 1000
    data = resp.json()
    prediction = data.get("prediction")
    run_id = data.get("run_id") or ""
    version = str(data.get("model_version", ""))
    log.info(
        "→ RES  job=%-8s  model=%s  prediction=%.2fW  version=%s  run=%s  latency=%.0fms",
        job.job_id[:8],
        model_name,
        prediction if prediction is not None else 0.0,
        version,
        run_id[:8] if run_id else "--------",
        latency_ms,
    )
    if prediction is not None:
        write_drift_snapshot(model_name, alias, float(prediction), str(job.job_id))
    embedding = features.get("embedding") or features.get("features", {}).get("embedding")
    if embedding:
        import math as _math
        vals = list(embedding)
        n = len(vals)
        emb_mean = sum(vals) / n
        emb_std = _math.sqrt(sum((v - emb_mean) ** 2 for v in vals) / n) if n > 1 else 0.0
        emb_norm = _math.sqrt(sum(v * v for v in vals))
        write_input_snapshot(model_name, alias, emb_norm, emb_mean, emb_std, str(job.job_id))
    write_audit_event("bridge", None, "inference_served", model_name,
                      {"alias": alias, "job_id": str(job.job_id)})
    return prediction, run_id, version


# ── pubsub handler ─────────────────────────────────────────────────────────────

async def _run_pubsub(conn: Connection) -> None:
    if JOB_TOPIC_UUID is None:
        log.warning("SEANERBUS_JOB_TOPIC_UUID not set — pubsub mode disabled")
        return

    log.info("Subscribing to job topic %s", JOB_TOPIC_UUID)
    result_conn: Connection | None = None
    if PUBLISH_RESULTS and RESULT_TOPIC_UUID is not None:
        result_conn = Connection(SEANERBUS_HOST, SEANERBUS_PORT)
        await result_conn.connect()
        log.info("Result publishing enabled → topic %s", RESULT_TOPIC_UUID)

    async for job in conn.subscribe(JOB_TOPIC_UUID, HpcJobV1):
        model = job.model_name or DEFAULT_MODEL
        alias = job.alias or DEFAULT_ALIAS
        error_msg = ""
        prediction: float | None = None
        run_id: str | None = None
        version: str | None = None

        try:
            prediction, run_id, version = await _call_pipeline(job)
            _drift_tracker.record(model, True)
            _bridge_stats["inferences_total"] += 1
            m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
            m["inferences"] += 1
        except ValueError as exc:
            # Schema misconfiguration — do NOT feed into drift tracker
            _ERRORS.labels(model=model).inc()
            error_msg = str(exc)
            log.error("Schema error handling pubsub job %s: %s", job.job_id, exc)
            _bridge_stats["inferences_total"] += 1
            m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
            m["inferences"] += 1
            m["errors"] += 1
        except httpx.HTTPError as exc:
            _ERRORS.labels(model=model).inc()
            error_msg = str(exc)
            log.error("Ray Serve error for job %s: %s", job.job_id, exc)
            _drift_tracker.record(model, False)
            _bridge_stats["inferences_total"] += 1
            m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
            m["inferences"] += 1
            m["errors"] += 1
        except Exception as exc:  # noqa: BLE001
            _ERRORS.labels(model=model).inc()
            error_msg = str(exc)
            log.error("Unexpected error handling pubsub job %s: %s", job.job_id, exc)
            _drift_tracker.record(model, False)
            _bridge_stats["inferences_total"] += 1
            m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
            m["inferences"] += 1
            m["errors"] += 1

        if result_conn is not None and RESULT_TOPIC_UUID is not None:
            result = HpcInferenceResV1(
                job_id=job.job_id,
                model_name=model,
                model_version=version or "",
                alias=alias,
                prediction=prediction if prediction is not None else 0.0,
                run_id=run_id or "",
                error_msg=error_msg,
            )
            try:
                await result_conn.publish(RESULT_TOPIC_UUID, result)
            except Exception as exc:  # noqa: BLE001
                log.error("Failed to publish result to seanerbus: %s", exc)


# ── req/res inference handler ──────────────────────────────────────────────────

async def _call_inference(req: HpcJobV1, override_model: str | None = None) -> HpcInferenceResV1:
    model = override_model or req.model_name or DEFAULT_MODEL
    alias = req.alias or DEFAULT_ALIAS

    try:
        prediction, run_id, version = await _call_pipeline(req, override_model=model)
        _drift_tracker.record(model, True)
        _bridge_stats["inferences_total"] += 1
        m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
        m["inferences"] += 1
        return HpcInferenceResV1(
            job_id=req.job_id,
            model_name=model,
            model_version=version or "",
            alias=alias,
            prediction=prediction if prediction is not None else 0.0,
            run_id=run_id or "",
        )
    except ValueError as exc:
        # Schema misconfiguration — do NOT feed into drift tracker
        _ERRORS.labels(model=model).inc()
        log.error("Schema error in req/res inference handler: %s", exc)
        _bridge_stats["inferences_total"] += 1
        m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
        m["inferences"] += 1
        m["errors"] += 1
        return HpcInferenceResV1(job_id=req.job_id, model_name=model, error_msg=str(exc))
    except httpx.HTTPError as exc:
        _ERRORS.labels(model=model).inc()
        log.error("Ray Serve error in req/res inference handler: %s", exc)
        _drift_tracker.record(model, False)
        _bridge_stats["inferences_total"] += 1
        m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
        m["inferences"] += 1
        m["errors"] += 1
        return HpcInferenceResV1(job_id=req.job_id, model_name=model, error_msg=str(exc))
    except Exception as exc:  # noqa: BLE001
        _ERRORS.labels(model=model).inc()
        log.error("Unexpected error in req/res inference handler: %s", exc)
        _drift_tracker.record(model, False)
        _bridge_stats["inferences_total"] += 1
        m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
        m["inferences"] += 1
        m["errors"] += 1
        return HpcInferenceResV1(job_id=req.job_id, model_name=model, error_msg=str(exc))


def _make_inference_handler(model_name: str):
    """Return an HpcJobV1 handler bound to model_name."""
    async def _handler(req: HpcJobV1) -> HpcInferenceResV1:
        return await _call_inference(req, override_model=model_name)
    return _handler


async def _handle_inference(job: HpcJobV1) -> HpcInferenceResV1:
    return await _call_inference(job)


# ── req/res retrain handler ────────────────────────────────────────────────────

async def _handle_retrain(req: RetrainReqV1) -> RetrainResV1:
    model = req.model_name or DEFAULT_MODEL
    dataset = req.dataset_name or "FDataDataset"
    backend = req.backend_name or "dataplane"

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if CONTROL_PLANE_TOKEN:
        headers["Authorization"] = f"Bearer {CONTROL_PLANE_TOKEN}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{CONTROL_PLANE_URL}/retrain",
                json={
                    "model_name": model,
                    "dataset_name": dataset,
                    "backend_name": backend,
                    "is_dummy": req.is_dummy,
                },
                headers=headers,
            )
            r.raise_for_status()
        data = r.json()
        flow_run_id = data.get("flow_run_id") or ""
        log.info("Retrain accepted for %s → flow_run_id=%s", model, flow_run_id)
        return RetrainResV1(flow_run_id=flow_run_id)
    except httpx.HTTPError as exc:
        log.error("Retrain request failed for %s: %s", model, exc)
        return RetrainResV1(error_msg=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.error("Unexpected error in retrain handler for %s: %s", model, exc)
        return RetrainResV1(error_msg=str(exc))


# ── req/res vector handler ─────────────────────────────────────────────────────
# VectorReqV1 carries raw float values with no num_nodes field, so it cannot
# go through the inference pipeline (/infer-pipeline/infer) which requires
# both embedding + num_nodes.  It calls MultiModelServer directly instead.

async def _handle_vector(req: VectorReqV1) -> VectorResV1:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{RAY_SERVE_URL}/predict/{DEFAULT_MODEL}",
                json={"features": {"embedding": list(req.values)}, "alias": DEFAULT_ALIAS},
            )
            resp.raise_for_status()
        prediction = resp.json().get("prediction", 0.0)
        log.info("Vector inference OK | model=%s prediction=%s", DEFAULT_MODEL, prediction)
        _bridge_stats["vectors_total"] += 1
        return VectorResV1(results=[prediction])
    except Exception as exc:  # noqa: BLE001
        log.error("Vector inference failed: %s", exc)
        _bridge_stats["vectors_total"] += 1
        return VectorResV1(results=[])


async def _run_reqres(inf_conn: Connection, retrain_conn: Connection, vector_conn: Connection) -> None:
    tasks: list[asyncio.Task] = []

    if MODEL_UUIDS:
        for model_name, model_uuid in MODEL_UUIDS.items():
            conn = Connection(SEANERBUS_HOST, SEANERBUS_PORT)
            await conn.connect()
            tasks.append(asyncio.create_task(
                conn.serve(model_uuid, HpcJobV1, _make_inference_handler(model_name))
            ))
            log.info("Registered per-model handler | model=%s uuid=%s", model_name, model_uuid)
    elif INFERENCE_UUID is not None:
        log.info("Registering global inference handler at %s (legacy)", INFERENCE_UUID)
        tasks.append(asyncio.create_task(
            inf_conn.serve(INFERENCE_UUID, HpcJobV1, _handle_inference)
        ))
    else:
        log.warning("No inference handlers registered — set SEANERBUS_INFERENCE_UUID or add seanerbus_uuid to model YAMLs")

    if RETRAIN_UUID is not None:
        log.info("Registering retrain handler at %s", RETRAIN_UUID)
        tasks.append(asyncio.create_task(
            retrain_conn.serve(RETRAIN_UUID, RetrainReqV1, _handle_retrain)
        ))
    if VECTOR_UUID is not None:
        log.info("Registering vector handler at %s", VECTOR_UUID)
        tasks.append(asyncio.create_task(
            vector_conn.serve(VECTOR_UUID, VectorReqV1, _handle_vector)
        ))

    if not tasks:
        log.warning("No req/res handlers configured — nothing to do in reqres mode")
        return

    await asyncio.gather(*tasks)


# ── entry point ────────────────────────────────────────────────────────────────

async def main() -> None:
    _BRIDGE_UP.set(1.0)
    _bridge_stats["mode"] = SEANERBUS_MODE
    log.info(
        "SeanerBUS bridge starting | host=%s port=%d mode=%s",
        SEANERBUS_HOST, SEANERBUS_PORT, SEANERBUS_MODE,
    )

    async def _run_bridge() -> None:
        if SEANERBUS_MODE == "pubsub":
            conn = Connection(SEANERBUS_HOST, SEANERBUS_PORT)
            await conn.connect()
            await _run_pubsub(conn)

        elif SEANERBUS_MODE == "reqres":
            inf_conn = Connection(SEANERBUS_HOST, SEANERBUS_PORT)
            retrain_conn = Connection(SEANERBUS_HOST, SEANERBUS_PORT)
            vector_conn = Connection(SEANERBUS_HOST, SEANERBUS_PORT)
            await inf_conn.connect()
            await retrain_conn.connect()
            await vector_conn.connect()
            await _run_reqres(inf_conn, retrain_conn, vector_conn)

        elif SEANERBUS_MODE == "both":
            pubsub_conn = Connection(SEANERBUS_HOST, SEANERBUS_PORT)
            inf_conn = Connection(SEANERBUS_HOST, SEANERBUS_PORT)
            retrain_conn = Connection(SEANERBUS_HOST, SEANERBUS_PORT)
            vector_conn = Connection(SEANERBUS_HOST, SEANERBUS_PORT)
            await asyncio.gather(
                pubsub_conn.connect(),
                inf_conn.connect(),
                retrain_conn.connect(),
                vector_conn.connect(),
            )
            await asyncio.gather(
                _run_pubsub(pubsub_conn),
                _run_reqres(inf_conn, retrain_conn, vector_conn),
            )

        else:
            log.error("Unknown SEANERBUS_MODE=%r — use pubsub | reqres | both", SEANERBUS_MODE)

    async def _run_bridge_safe() -> None:
        """Run bridge with auto-reconnect; keeps status server alive on failures."""
        backoff = 2.0
        attempt = 0
        while True:
            attempt += 1
            try:
                if attempt > 1:
                    log.info("Reconnecting to SeanerBUS (attempt %d) …", attempt)
                await _run_bridge()
                log.info("Bridge exited cleanly")
                break
            except EOFError as exc:
                log.warning("SeanerBUS connection closed: %s — reconnecting in %.0fs", exc, backoff)
            except Exception:
                log.exception("Bridge connection failed — reconnecting in %.0fs", backoff)
            _bridge_stats["bridge_error"] = True
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    await asyncio.gather(run_status_server(), _run_bridge_safe())


if __name__ == "__main__":
    asyncio.run(capnp.run(main()))
