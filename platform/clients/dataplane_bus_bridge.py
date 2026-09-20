"""
Dataplane bus → ExaMLOps Ray Serve bridge.

Connects a Cap'n Proto / TCP dataplane-bus server to ExaMLOps's Ray Serve inference
API. Supports three modes via DATAPLANE_BUS_MODE:

  pubsub   — subscribe to HpcJobV1 messages, call Ray Serve, optionally publish
             HpcInferenceResV1 results back to a result topic
  reqres   — register two handlers: inference (HpcJobV1 → HpcInferenceResV1)
             and retrain trigger (RetrainReqV1 → RetrainResV1)
  both     — run all of the above concurrently, each on its own Connection

Env vars (defaults shown):
    DATAPLANE_BUS_HOST=localhost
    DATAPLANE_BUS_PORT=5398
    DATAPLANE_BUS_MODE=both
    DATAPLANE_BUS_JOB_TOPIC_UUID        required for pubsub / both
    DATAPLANE_BUS_RESULT_TOPIC_UUID     optional; enables result publishing
    DATAPLANE_BUS_INFERENCE_UUID        required for reqres / both
    DATAPLANE_BUS_RETRAIN_UUID          required for reqres / both
    DATAPLANE_BUS_VECTOR_UUID           required for vector req/res; omit to disable
    RAY_SERVE_URL=http://localhost:18001
    CONTROL_PLANE_URL=http://localhost:18002
    CONTROL_PLANE_TOKEN=
    DATAPLANE_BUS_DEFAULT_MODEL=JPCP
    DATAPLANE_BUS_DEFAULT_ALIAS=Production
    DATAPLANE_BUS_PUBLISH_RESULTS=true
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

# Allow importing sibling packages when run as `python clients/dataplane_bus_bridge.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_CLI_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cli", "src")
sys.path.insert(0, os.path.abspath(_CLI_SRC))

try:
    # Not via `platform_db`: that module's surface is frozen (the coupling ratchet).
    from examlops.data.audit import audit_best_effort
    from examlops.platform_db import (
        get_input_baseline,
        write_audit_event,
        write_drift_snapshot,
        write_input_snapshot,
    )
    from examlops.platform_db import (
        init_db as _init_platform_db,
    )

    _init_platform_db()
    _PLATFORM_DB_AVAILABLE = True
except Exception:
    _PLATFORM_DB_AVAILABLE = False

    def write_drift_snapshot(*a, **k):
        pass  # type: ignore[misc]

    def write_audit_event(*a, **k):
        pass  # type: ignore[misc]

    def audit_best_effort(*a, **k):  # type: ignore[misc]
        return False

    def write_input_snapshot(*a, **k):
        pass  # type: ignore[misc]

    def get_input_baseline(*a, **k):  # type: ignore[misc]
        return None


try:
    from examlops.resilience import httpx_timeout as _httpx_timeout
except Exception:

    def _httpx_timeout(read=None, connect=None):  # type: ignore[misc]
        return httpx.Timeout(10.0)


def _control_plane_headers() -> dict[str, str]:
    """JSON + the bridge's bearer + a fresh ``Idempotency-Key``: a retried submission resolves to
    the same control-plane command instead of a second retrain (plan P1.7)."""
    headers = {"Content-Type": "application/json", "Idempotency-Key": uuid.uuid4().hex}
    from examlops.service_auth import control_plane_bearer  # noqa: PLC0415

    token = control_plane_bearer(CONTROL_PLANE_TOKEN)  # CONTROL_PLANE_TOKEN_FILE first (ADR 0125)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _json_field(response: httpx.Response, field: str) -> object:
    try:
        return response.json().get(field)
    except Exception:  # noqa: BLE001 - an audit detail, never worth failing the path
        return None


async def _follow_retrain(client: httpx.AsyncClient, view: dict, headers: dict[str, str]) -> dict:
    """Wait for a submitted retrain command to be dispatched (``examlops.retrain_command``).

    Without the examlops package the 202 answer is returned as is: the retrain is accepted and
    the control plane dispatches it; the requester just gets the command's URL instead of a run.
    """
    try:
        from examlops import retrain_command
    except Exception:  # noqa: BLE001
        return {"command_id": view.get("command_id"), "status_url": view.get("status_url")}

    async def fetch(command_id: str) -> dict:
        r = await client.get(f"{CONTROL_PLANE_URL}/v1/commands/{command_id}", headers=headers)
        r.raise_for_status()
        return r.json()

    try:
        # A bus requester waits for this reply, so wait the short automation budget, not 30 s.
        return await retrain_command.follow_async(
            view, fetch, wait=retrain_command.AUTOMATION_WAIT_SECONDS
        )
    except httpx.HTTPError:
        # Accepted, then following it failed: the command stands, so report it as accepted.
        return retrain_command.outcome(view)


from dataplane_bus_client import Connection  # noqa: E402
from dataplane_bus_msgs import (  # noqa: E402
    HpcInferenceResV1,
    HpcJobV1,
    RetrainReqV1,
    RetrainResV1,
    VectorReqV1,
    VectorResV1,
)
from model_schema_registry import ModelSchemaRegistry  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [dataplane-bus-bridge] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dataplane_bus_bridge")
logging.getLogger("httpx").setLevel(logging.WARNING)  # suppress per-request HTTP noise

# ── configuration ──────────────────────────────────────────────────────────────

DATAPLANE_BUS_HOST = os.getenv("DATAPLANE_BUS_HOST", "localhost")
DATAPLANE_BUS_PORT = int(os.getenv("DATAPLANE_BUS_PORT", "5398"))
DATAPLANE_BUS_MODE = os.getenv("DATAPLANE_BUS_MODE", "both").lower()

RAY_SERVE_URL = os.getenv("RAY_SERVE_URL", "http://localhost:18001").rstrip("/")
CONTROL_PLANE_URL = os.getenv("CONTROL_PLANE_URL", "http://localhost:18002").rstrip("/")
CONTROL_PLANE_TOKEN = os.getenv("CONTROL_PLANE_TOKEN", "")

DEFAULT_MODEL = os.getenv("DATAPLANE_BUS_DEFAULT_MODEL", "JPCP")
DEFAULT_ALIAS = os.getenv("DATAPLANE_BUS_DEFAULT_ALIAS", "Production")
PUBLISH_RESULTS = os.getenv("DATAPLANE_BUS_PUBLISH_RESULTS", "true").lower() == "true"

DRIFT_WINDOW = int(os.getenv("DRIFT_WINDOW", os.getenv("CLIENT_SIM_DRIFT_WINDOW", "50")))
DRIFT_THRESHOLD = float(
    os.getenv("DRIFT_THRESHOLD", os.getenv("CLIENT_SIM_DRIFT_THRESHOLD", "0.5"))
)
DRIFT_COOLDOWN = int(os.getenv("DRIFT_COOLDOWN", os.getenv("CLIENT_SIM_DRIFT_COOLDOWN", "300")))
MODELS_YAML_DIR = os.getenv("MODELS_YAML_DIR", "")

_schema_registry = ModelSchemaRegistry(MODELS_YAML_DIR if MODELS_YAML_DIR else None)


def _parse_uuid(env_var: str) -> uuid.UUID | None:
    raw = os.getenv(env_var, "")
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        return None


JOB_TOPIC_UUID = _parse_uuid("DATAPLANE_BUS_JOB_TOPIC_UUID")
RESULT_TOPIC_UUID = _parse_uuid("DATAPLANE_BUS_RESULT_TOPIC_UUID")
INFERENCE_UUID = _parse_uuid("DATAPLANE_BUS_INFERENCE_UUID")
RETRAIN_UUID = _parse_uuid("DATAPLANE_BUS_RETRAIN_UUID")
VECTOR_UUID = _parse_uuid("DATAPLANE_BUS_VECTOR_UUID")


def _load_model_uuids() -> dict[str, uuid.UUID]:
    """Read dataplane_bus_uuid from every model YAML in MODELS_YAML_DIR.

    Returns {UPPERCASE_MODEL_NAME: uuid.UUID}. Models without dataplane_bus_uuid
    are silently skipped. Errors in individual files are logged and skipped.
    """
    if not MODELS_YAML_DIR:
        return {}
    from pathlib import Path

    import yaml as _yaml

    models_path = Path(MODELS_YAML_DIR)
    if not models_path.is_dir():
        log.warning(
            "MODELS_YAML_DIR=%r is not a directory — per-model UUIDs disabled", MODELS_YAML_DIR
        )
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
        raw_uuid = raw.get("dataplane_bus_uuid", "")
        model_name = raw.get("name", "")
        if raw_uuid and model_name:
            try:
                result[model_name.upper()] = uuid.UUID(raw_uuid)
            except ValueError:
                log.warning("Invalid dataplane_bus_uuid in %s — skipping", yaml_file.name)
    return result


MODEL_UUIDS: dict[str, uuid.UUID] = _load_model_uuids()

# ── Prometheus metrics ─────────────────────────────────────────────────────────

_BRIDGE_UP = Gauge(
    "dataplane_bus_bridge_up",
    "Set to 1.0 while the bridge process is running",
)
_INFERENCES = Counter(
    "dataplane_bus_inferences_total",
    "Total inference calls dispatched to Ray Serve",
    ["model"],
)
_ERRORS = Counter(
    "dataplane_bus_inference_errors_total",
    "Total inference calls that returned an error",
    ["model"],
)
_LATENCY = Histogram(
    "dataplane_bus_inference_latency_seconds",
    "End-to-end inference latency from bridge POST to Ray Serve response",
    ["model"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)
_RETRAINS = Counter(
    "dataplane_bus_retrain_triggers_total",
    "Total drift-triggered retrains posted to the Control Plane",
)
# Per-inference telemetry is written off the reply path (plan P0.5 / finding B5). These make the
# cost of that decision visible instead of silent: what was dropped, what failed, what is waiting.
_TELEMETRY_DROPPED = Counter(
    "dataplane_bus_telemetry_dropped_total",
    "Inference telemetry records dropped because the persistence spool was full",
)
_TELEMETRY_FAILURES = Counter(
    "dataplane_bus_telemetry_persist_failures_total",
    "Inference telemetry records whose database write failed (the inference itself succeeded)",
)
_TELEMETRY_DEPTH = Gauge(
    "dataplane_bus_telemetry_queue_depth",
    "Inference telemetry records waiting to be written",
)
_TELEMETRY_EVENTBUS_FAILURES = Counter(
    "dataplane_bus_telemetry_eventbus_publish_failures_total",
    "Inference telemetry events that failed to publish to the event backbone "
    "(EXAMLOPS_TELEMETRY_VIA_EVENTBUS) and were dropped rather than written directly",
)

# Input-embedding drift (phase 21). The bridge already computes norm/mean/std on every
# inference to write `input_snapshots`; it just never exported them, so the three Grafana
# panels built on these names rendered "No data" from the day they shipped — which reads as a
# calm system, not as a missing exporter. Labelled `model`, like every other metric here.
_EMB_NORM = Gauge(
    "dataplane_bus_embedding_norm",
    "L2 norm of the most recent input embedding, per model",
    ["model"],
)
_EMB_MEAN = Gauge(
    "dataplane_bus_embedding_mean",
    "Mean of the most recent input embedding, per model",
    ["model"],
)
_EMB_STD = Gauge(
    "dataplane_bus_embedding_std",
    "Standard deviation of the most recent input embedding, per model",
    ["model"],
)
# The baselines are what the live values are *drift from*, so a panel without them shows a
# line with nothing to judge it against.
_EMB_NORM_BASELINE = Gauge(
    "dataplane_bus_embedding_norm_baseline",
    "Recorded baseline embedding norm, per model (exa drift input baseline)",
    ["model"],
)
_EMB_MEAN_BASELINE = Gauge(
    "dataplane_bus_embedding_mean_baseline",
    "Recorded baseline embedding mean, per model (exa drift input baseline)",
    ["model"],
)
_EMB_STD_BASELINE = Gauge(
    "dataplane_bus_embedding_std_baseline",
    "Recorded baseline embedding std, per model (exa drift input baseline)",
    ["model"],
)

# A baseline changes only when someone runs `exa drift input baseline`, so re-reading it on
# every inference would be a SQLite hit per request for a value that is near-constant.
_BASELINE_TTL_SECONDS = 60.0
_baseline_seen_at: dict[str, float] = {}


def _publish_input_baseline(model_name: str) -> None:
    """Refresh the baseline gauges for one model, at most once per TTL. Never raises."""
    now = time.monotonic()
    if now - _baseline_seen_at.get(model_name, float("-inf")) < _BASELINE_TTL_SECONDS:
        return
    _baseline_seen_at[model_name] = now
    try:
        stats = get_input_baseline(model_name)
    except Exception:
        return
    if not stats:
        # No baseline recorded yet. Leave the gauges unset rather than publishing a zero,
        # which would draw a floor on the panel and read as a real measurement.
        return
    for key, gauge in (
        ("norm_mean", _EMB_NORM_BASELINE),
        ("mean_mean", _EMB_MEAN_BASELINE),
        ("std_mean", _EMB_STD_BASELINE),
    ):
        value = stats.get(key)
        if value is not None:
            gauge.labels(model=model_name).set(float(value))


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


class ModelInferenceError(RuntimeError):
    """The ingress served the request but the MODEL failed to produce a prediction.

    Raised when the pipeline answers 5xx with ``{"error": "inference_failed"}`` and ``cause``
    ``model`` (or no cause, from an older pipeline) — a model-quality signal that MUST reach the
    drift tracker. Distinct from transport failures
    (connect/timeout/unreachable), which stay excluded so an outage never masquerades as drift.
    Without this distinction the tracker could never see a model failure at all: the ingress
    converts them to HTTP 500 and ``raise_for_status`` turned every one into an excluded
    ``httpx.HTTPError``, so the error rate was permanently 0 and drift-retrain was dead code.
    """


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
        # Keep strong references to fire-and-forget trigger tasks so the event loop
        # can't garbage-collect them mid-flight (a silently-dropped retrain trigger).
        self._tasks: set[asyncio.Task] = set()

    def record(self, model: str, success: bool) -> None:
        """Record a genuine model-inference outcome.

        IMPORTANT: only feed *model-quality* signals here. Infrastructure/transport
        failures (Ray Serve down, timeouts, connection errors) must NOT be recorded —
        otherwise an outage inflates the error rate and spuriously triggers retrains.
        """
        bucket = self._results.setdefault(model, [])
        bucket.append(success)
        if len(bucket) > self.window:
            del bucket[0]
        if len(bucket) >= self.window and self._error_rate(bucket) >= self.threshold:
            task = asyncio.create_task(self._maybe_trigger(model, self._error_rate(bucket)))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _error_rate(self, bucket: list[bool]) -> float:
        return sum(1 for s in bucket if not s) / len(bucket)

    async def _maybe_trigger(self, model: str, rate: float) -> None:
        now = time.time()
        if now - self._last_retrain.get(model, 0) < self.cooldown:
            return
        self._last_retrain[model] = now
        log.warning(
            "Drift detected for %s (error_rate=%.0f%%) — triggering retrain", model, rate * 100
        )
        headers = _control_plane_headers()
        try:
            async with httpx.AsyncClient(timeout=_httpx_timeout()) as client:
                # The command API (plan P1.6c): 202 means the retrain is durably accepted and the
                # control plane dispatches it — this path needs no flow run id, so it does not wait.
                r = await client.post(
                    f"{CONTROL_PLANE_URL}/v1/retrain",
                    json={
                        "model_name": model,
                        "dataset_name": "FDataDataset",
                        "backend_name": "dataplane",
                        "is_dummy": False,
                    },
                    headers=headers,
                )
            if r.status_code >= 400:
                # A rejected trigger (bad token, control plane 503) is NOT a retrain: don't
                # count it, don't audit it as one, and release the cooldown so the next window
                # breach retries instead of silently waiting out a cooldown nothing earned.
                log.error("Retrain trigger REJECTED for %s → HTTP %d", model, r.status_code)
                self._last_retrain.pop(model, None)
                await asyncio.to_thread(
                    write_audit_event,
                    "bridge",
                    None,
                    "retrain_trigger_failed",
                    model,
                    {
                        "reason": "drift",
                        "error_rate": round(rate, 4),
                        "http_status": r.status_code,
                    },
                )
                return
            log.info("Retrain triggered for %s → HTTP %d", model, r.status_code)
            _RETRAINS.inc()
            _bridge_stats["retrains_total"] += 1
            # No human is present on this path — the bridge decides to retrain from live error
            # rates — so it is the door that most needs a trace. The control plane cannot write
            # it: it holds no platform.db. Off-loop, like every other SQLite write here.
            await asyncio.to_thread(
                write_audit_event,
                "bridge",
                None,
                "retrain_triggered",
                model,
                {
                    "reason": "drift",
                    "error_rate": round(rate, 4),
                    "dataset": "FDataDataset",
                    "http_status": r.status_code,
                    "command_id": _json_field(r, "command_id"),
                },
            )
        except httpx.HTTPError as exc:
            log.error("Retrain trigger failed for %s: %s", model, exc)
            self._last_retrain.pop(model, None)


_drift_tracker = DriftTracker()


# One long-lived HTTP client for the bridge→ingress hop: a fresh AsyncClient per inference pays
# TCP + pool construction on the hot path (the bus can deliver jobs at line rate).
_http_client: httpx.AsyncClient | None = None


def _shared_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=_httpx_timeout())
    return _http_client


# ── inference pipeline call ────────────────────────────────────────────────────


async def _call_pipeline(
    job: HpcJobV1, override_model: str | None = None
) -> tuple[float | None, str | None, str | None]:
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
    client = _shared_http_client()
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
    if resp.status_code >= 400:
        # Classify before raise_for_status: an ingress 5xx carrying inference_failed is the
        # MODEL failing (drift signal), an ingress 422 is a schema problem (ValueError path);
        # everything else stays a transport/HTTP error excluded from the drift tracker.
        detail: Any = None
        try:
            detail = resp.json()
        except Exception:  # noqa: BLE001
            detail = None
        if isinstance(detail, dict):
            # The pipeline says why (`cause`). Only `model` is the model failing; an answer
            # without a cause comes from an older pipeline and keeps the old meaning. Any other
            # cause (transport, replica_lost, timeout, protocol, pipeline) is infrastructure:
            # raise_for_status below sends it down the path that never feeds the drift tracker.
            if (
                detail.get("error") == "inference_failed"
                and detail.get("cause", "model") == "model"
            ):
                raise ModelInferenceError(str(detail.get("detail") or "inference_failed"))
            if detail.get("error") == "validation_error":
                raise ValueError(str(detail.get("detail") or "validation_error"))
        resp.raise_for_status()
    latency_s = time.perf_counter() - t0
    # `_INFERENCES` is NOT incremented here. This line is past `raise_for_status()`, so counting
    # it here counted successes only — while the metric's name, its help text and the bridge's own
    # `_bridge_stats["inferences_total"]` all mean *every dispatched call*. Everything dividing
    # errors by it therefore computed errors ÷ successes: 90 % failures read as 900 %, and a total
    # outage as +Inf. It is now incremented beside each `inferences_total` below, which is the one
    # place that decides what a dispatched call is. Latency stays here: only a served response has
    # one.
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
    embedding = features.get("embedding") or features.get("features", {}).get("embedding")
    # Reply first (plan P0.5 / finding B5). The drift and input-embedding writes used to be awaited
    # here, so a locked or unreachable database turned a successful prediction into an error on the
    # bus and added up to busy_timeout x retries of latency to every job. They are now handed to a
    # bounded spool that a background worker drains; this call never waits on the database.
    _telemetry_spool.offer((model_name, alias, prediction, embedding, str(job.job_id)))
    return prediction, run_id, version


class _TelemetrySpool:
    """A bounded, drop-on-full queue of per-inference telemetry, drained by one worker task.

    Bounded because an unbounded buffer converts a slow database into a memory leak. Dropping is
    the right overflow policy for this data: drift and input-embedding statistics are windowed
    aggregates, so a lost sample shifts nothing an operator acts on, while a blocked reply stalls a
    scheduler waiting on the bus. Every drop and every failed write is counted.
    """

    def __init__(self, maxsize: int) -> None:
        self._maxsize = max(1, maxsize)
        self._queue: asyncio.Queue[tuple] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def offer(self, record: tuple) -> bool:
        loop = asyncio.get_running_loop()
        # A queue and its worker belong to the loop that created them. If the running loop has
        # changed (a reconnect under a fresh `asyncio.run`, a test harness), start over on this
        # one: records queued on a dead loop would never be drained.
        if self._queue is None or self._loop is not loop:
            self._queue = asyncio.Queue(maxsize=self._maxsize)
            self._loop = loop
            self._worker = None
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._drain())
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            _TELEMETRY_DROPPED.inc()
            return False
        _TELEMETRY_DEPTH.set(self._queue.qsize())
        return True

    async def _drain(self) -> None:
        assert self._queue is not None
        while True:
            record = await self._queue.get()
            try:
                await asyncio.to_thread(_persist_inference_telemetry, *record)
            except Exception as exc:  # noqa: BLE001 - telemetry must never take the bridge down
                _TELEMETRY_FAILURES.inc()
                log.warning("Inference telemetry write failed (inference unaffected): %s", exc)
            finally:
                self._queue.task_done()
                _TELEMETRY_DEPTH.set(self._queue.qsize())

    async def join(self) -> None:
        """Wait until every offered record has been written or failed (tests, shutdown)."""
        if self._queue is not None:
            await self._queue.join()


_telemetry_spool = _TelemetrySpool(int(os.getenv("DATAPLANE_BUS_TELEMETRY_QUEUE_MAX", "1000")))


def _telemetry_via_eventbus() -> bool:
    return os.getenv("EXAMLOPS_TELEMETRY_VIA_EVENTBUS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _publish_inference_telemetry_event(
    model_name: str,
    alias: str,
    prediction: float | None,
    job_id: str,
    embedding_stats: dict[str, float] | None,
) -> None:
    """Publish, never write directly (ADR 0123 decision 4): the serving plane crosses the
    boundary to the control plane only as an event, never a transactional database write.

    Deliberately **not** the durable outbox (:func:`examlops.events.publish`) that
    ``model.alias_changed`` and similar events use: that path itself writes to ``platform.db``
    first (it enqueues, a relay drains it later), which would defeat the point — the bridge would
    still need database connectivity. This publishes straight to NATS JetStream, the same direct
    call :mod:`examlops.serving_snapshot` already makes for its own high-volume, best-effort data.
    A publish failure is counted and the record is dropped, exactly like a full spool today: these
    are windowed aggregates, so a lost sample shifts nothing an operator acts on, and falling back
    to a direct database write here would silently re-couple the serving plane to platform.db the
    moment NATS has a bad moment — the one thing this mode exists to avoid.
    """
    from examlops.events import NatsPublisher  # noqa: PLC0415

    payload: dict[str, object] = {"model": model_name, "alias": alias, "job_id": job_id}
    if prediction is not None:
        payload["prediction"] = prediction
    if embedding_stats is not None:
        payload["embedding_stats"] = embedding_stats
    try:
        NatsPublisher().publish(
            "serving.inference_telemetry", payload, event_id=f"telemetry:{job_id}"
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never take the bridge down
        _TELEMETRY_EVENTBUS_FAILURES.inc()
        log.warning("Inference telemetry event publish failed (dropped, not retried): %s", exc)


def _persist_inference_telemetry(
    model_name: str,
    alias: str,
    prediction: float | None,
    embedding: object,
    job_id: str,
) -> None:
    """The per-inference drift / input-embedding writes, run by the telemetry spool's worker.

    There is deliberately no per-inference audit event. It used to append an ``inference_served``
    row to the hash-chained audit log for every prediction: nothing read those rows, retention
    excludes the audit chain so they were never pruned, and each append took the platform-wide
    audit lock on the hot path. The audit log records decisions; request volume is
    ``dataplane_bus_inferences_total``.

    ``EXAMLOPS_TELEMETRY_VIA_EVENTBUS`` (default off) routes the database writes through the
    event backbone instead of calling them here directly — see
    :func:`_publish_inference_telemetry_event`. Gauges and the input-drift baseline publish stay
    local regardless: they are this process's own instrumentation, not a control-plane write.
    """
    via_eventbus = _telemetry_via_eventbus()
    embedding_stats: dict[str, float] | None = None
    if embedding:
        import math as _math

        vals = list(embedding)  # type: ignore[call-overload]
        n = len(vals)
        emb_mean = sum(vals) / n
        emb_std = _math.sqrt(sum((v - emb_mean) ** 2 for v in vals) / n) if n > 1 else 0.0
        emb_norm = _math.sqrt(sum(v * v for v in vals))
        embedding_stats = {"norm": emb_norm, "mean": emb_mean, "std": emb_std}
        if not via_eventbus:
            write_input_snapshot(model_name, alias, emb_norm, emb_mean, emb_std, job_id)
        _EMB_NORM.labels(model=model_name).set(emb_norm)
        _EMB_MEAN.labels(model=model_name).set(emb_mean)
        _EMB_STD.labels(model=model_name).set(emb_std)
        _publish_input_baseline(model_name)
    if not via_eventbus and prediction is not None:
        write_drift_snapshot(model_name, alias, float(prediction), job_id)
    if via_eventbus and (prediction is not None or embedding_stats is not None):
        _publish_inference_telemetry_event(model_name, alias, prediction, job_id, embedding_stats)


# ── pubsub handler ─────────────────────────────────────────────────────────────


async def _run_pubsub(conn: Connection) -> None:
    if JOB_TOPIC_UUID is None:
        log.warning("DATAPLANE_BUS_JOB_TOPIC_UUID not set — pubsub mode disabled")
        return

    log.info("Subscribing to job topic %s", JOB_TOPIC_UUID)
    result_conn: Connection | None = None
    if PUBLISH_RESULTS and RESULT_TOPIC_UUID is not None:
        result_conn = Connection(DATAPLANE_BUS_HOST, DATAPLANE_BUS_PORT)
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
            # Drift is a MODEL-quality signal: a served response with no prediction
            # is a genuine inference failure (record False); a real prediction is a
            # success. Transport failures below never reach the tracker.
            _drift_tracker.record(model, prediction is not None)
            _INFERENCES.labels(model=model).inc()
            _bridge_stats["inferences_total"] += 1
            m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
            m["inferences"] += 1
            if prediction is None:
                m["errors"] += 1
        except ModelInferenceError as exc:
            # The model itself failed — the one error class that IS a drift signal.
            _drift_tracker.record(model, False)
            _ERRORS.labels(model=model).inc()
            error_msg = str(exc)
            log.error("Model inference failed for pubsub job %s: %s", job.job_id, exc)
            _INFERENCES.labels(model=model).inc()
            _bridge_stats["inferences_total"] += 1
            m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
            m["inferences"] += 1
            m["errors"] += 1
        except ValueError as exc:
            # Schema misconfiguration — do NOT feed into drift tracker
            _ERRORS.labels(model=model).inc()
            error_msg = str(exc)
            log.error("Schema error handling pubsub job %s: %s", job.job_id, exc)
            _INFERENCES.labels(model=model).inc()
            _bridge_stats["inferences_total"] += 1
            m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
            m["inferences"] += 1
            m["errors"] += 1
        except httpx.HTTPError as exc:
            # Infrastructure/transport failure (Ray Serve down, timeout, 5xx) — count
            # as an error but do NOT feed the drift tracker, else an outage would
            # masquerade as model drift and spuriously trigger retrains.
            _ERRORS.labels(model=model).inc()
            error_msg = str(exc)
            log.error("Ray Serve transport error for job %s: %s", job.job_id, exc)
            _INFERENCES.labels(model=model).inc()
            _bridge_stats["inferences_total"] += 1
            m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
            m["inferences"] += 1
            m["errors"] += 1
        except Exception as exc:  # noqa: BLE001
            # Unexpected/infrastructure failure — likewise excluded from drift.
            _ERRORS.labels(model=model).inc()
            error_msg = str(exc)
            log.error("Unexpected error handling pubsub job %s: %s", job.job_id, exc)
            _INFERENCES.labels(model=model).inc()
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
                log.error("Failed to publish result to dataplane-bus: %s", exc)


# ── req/res inference handler ──────────────────────────────────────────────────


async def _call_inference(req: HpcJobV1, override_model: str | None = None) -> HpcInferenceResV1:
    model = override_model or req.model_name or DEFAULT_MODEL
    alias = req.alias or DEFAULT_ALIAS

    try:
        prediction, run_id, version = await _call_pipeline(req, override_model=model)
        # Model-quality signal only: no prediction on a served response = failure.
        _drift_tracker.record(model, prediction is not None)
        _INFERENCES.labels(model=model).inc()
        _bridge_stats["inferences_total"] += 1
        m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
        m["inferences"] += 1
        if prediction is None:
            m["errors"] += 1
        return HpcInferenceResV1(
            job_id=req.job_id,
            model_name=model,
            model_version=version or "",
            alias=alias,
            prediction=prediction if prediction is not None else 0.0,
            run_id=run_id or "",
        )
    except ModelInferenceError as exc:
        # The model itself failed — the one error class that IS a drift signal.
        _drift_tracker.record(model, False)
        _ERRORS.labels(model=model).inc()
        log.error("Model inference failed in req/res inference handler: %s", exc)
        _INFERENCES.labels(model=model).inc()
        _bridge_stats["inferences_total"] += 1
        m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
        m["inferences"] += 1
        m["errors"] += 1
        return HpcInferenceResV1(job_id=req.job_id, model_name=model, error_msg=str(exc))
    except ValueError as exc:
        # Schema misconfiguration — do NOT feed into drift tracker
        _ERRORS.labels(model=model).inc()
        log.error("Schema error in req/res inference handler: %s", exc)
        _INFERENCES.labels(model=model).inc()
        _bridge_stats["inferences_total"] += 1
        m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
        m["inferences"] += 1
        m["errors"] += 1
        return HpcInferenceResV1(job_id=req.job_id, model_name=model, error_msg=str(exc))
    except httpx.HTTPError as exc:
        # Infrastructure/transport failure — excluded from drift (see _run_pubsub).
        _ERRORS.labels(model=model).inc()
        log.error("Ray Serve transport error in req/res inference handler: %s", exc)
        _INFERENCES.labels(model=model).inc()
        _bridge_stats["inferences_total"] += 1
        m = _bridge_stats["per_model"].setdefault(model, {"inferences": 0, "errors": 0})
        m["inferences"] += 1
        m["errors"] += 1
        return HpcInferenceResV1(job_id=req.job_id, model_name=model, error_msg=str(exc))
    except Exception as exc:  # noqa: BLE001
        # Unexpected/infrastructure failure — likewise excluded from drift.
        _ERRORS.labels(model=model).inc()
        log.error("Unexpected error in req/res inference handler: %s", exc)
        _INFERENCES.labels(model=model).inc()
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

    headers = _control_plane_headers()

    try:
        async with httpx.AsyncClient(timeout=_httpx_timeout()) as client:
            # The command API (plan P1.6c), waited on until dispatched: the requester gets the
            # flow run id when the dispatch happens in time, otherwise the command to follow.
            r = await client.post(
                f"{CONTROL_PLANE_URL}/v1/retrain",
                json={
                    "model_name": model,
                    "dataset_name": dataset,
                    "backend_name": backend,
                    "is_dummy": req.is_dummy,
                },
                headers=headers,
            )
            r.raise_for_status()
            data = await _follow_retrain(client, r.json(), headers)
        flow_run_id = data.get("flow_run_id") or ""
        log.info(
            "Retrain accepted for %s → flow_run_id=%s command=%s",
            model,
            flow_run_id or "(not dispatched yet)",
            data.get("command_id"),
        )
        # `audit_best_effort`, not `write_audit_event`: this sits inside the same `try` as the
        # control-plane POST, whose handler answers the bus with `error_msg`. A raise here reported
        # a retrain the control plane had ACCEPTED as failed, and a caller that retries on error
        # would fire a second retrain of the same model on the cluster. The helper logs and counts
        # the lost event instead.
        await asyncio.to_thread(
            audit_best_effort,
            "bridge",
            None,
            "retrain_triggered",
            model,
            {
                "reason": "bus_request",
                "dataset": dataset,
                "backend": backend,
                "is_dummy": bool(req.is_dummy),
                "flow_run_id": flow_run_id,
                "command_id": data.get("command_id"),
            },
        )
        return RetrainResV1(flow_run_id=flow_run_id, status_url=data.get("status_url") or "")
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
        async with httpx.AsyncClient(timeout=_httpx_timeout()) as client:
            # Open Inference Protocol v2 (ADR 0126); /predict is deprecated.
            from examlops import oip_client

            resp = await client.post(
                f"{RAY_SERVE_URL}{oip_client.infer_path(DEFAULT_MODEL)}",
                json=oip_client.features_request(
                    {"embedding": list(req.values)}, alias=DEFAULT_ALIAS
                ),
            )
            resp.raise_for_status()
        prediction = oip_client.result(resp.json())["prediction"]
        log.info("Vector inference OK | model=%s prediction=%s", DEFAULT_MODEL, prediction)
        _bridge_stats["vectors_total"] += 1
        return VectorResV1(results=[prediction])
    except Exception as exc:  # noqa: BLE001
        log.error("Vector inference failed: %s", exc)
        _bridge_stats["vectors_total"] += 1
        return VectorResV1(results=[])


async def _run_reqres(
    inf_conn: Connection, retrain_conn: Connection, vector_conn: Connection
) -> None:
    tasks: list[asyncio.Task] = []

    if MODEL_UUIDS:
        for model_name, model_uuid in MODEL_UUIDS.items():
            conn = Connection(DATAPLANE_BUS_HOST, DATAPLANE_BUS_PORT)
            await conn.connect()
            tasks.append(
                asyncio.create_task(
                    conn.serve(model_uuid, HpcJobV1, _make_inference_handler(model_name))
                )
            )
            log.info("Registered per-model handler | model=%s uuid=%s", model_name, model_uuid)
    elif INFERENCE_UUID is not None:
        log.info("Registering global inference handler at %s (legacy)", INFERENCE_UUID)
        tasks.append(
            asyncio.create_task(inf_conn.serve(INFERENCE_UUID, HpcJobV1, _handle_inference))
        )
    else:
        log.warning(
            "No inference handlers registered — set DATAPLANE_BUS_INFERENCE_UUID or add dataplane_bus_uuid to model YAMLs"
        )

    if RETRAIN_UUID is not None:
        log.info("Registering retrain handler at %s", RETRAIN_UUID)
        tasks.append(
            asyncio.create_task(retrain_conn.serve(RETRAIN_UUID, RetrainReqV1, _handle_retrain))
        )
    if VECTOR_UUID is not None:
        log.info("Registering vector handler at %s", VECTOR_UUID)
        tasks.append(
            asyncio.create_task(vector_conn.serve(VECTOR_UUID, VectorReqV1, _handle_vector))
        )

    if not tasks:
        log.warning("No req/res handlers configured — nothing to do in reqres mode")
        return

    await asyncio.gather(*tasks)


# ── entry point ────────────────────────────────────────────────────────────────


async def main() -> None:
    _BRIDGE_UP.set(1.0)
    _bridge_stats["mode"] = DATAPLANE_BUS_MODE
    log.info(
        "Dataplane bus bridge starting | host=%s port=%d mode=%s",
        DATAPLANE_BUS_HOST,
        DATAPLANE_BUS_PORT,
        DATAPLANE_BUS_MODE,
    )

    async def _run_bridge() -> None:
        if DATAPLANE_BUS_MODE == "pubsub":
            conn = Connection(DATAPLANE_BUS_HOST, DATAPLANE_BUS_PORT)
            await conn.connect()
            await _run_pubsub(conn)

        elif DATAPLANE_BUS_MODE == "reqres":
            inf_conn = Connection(DATAPLANE_BUS_HOST, DATAPLANE_BUS_PORT)
            retrain_conn = Connection(DATAPLANE_BUS_HOST, DATAPLANE_BUS_PORT)
            vector_conn = Connection(DATAPLANE_BUS_HOST, DATAPLANE_BUS_PORT)
            await inf_conn.connect()
            await retrain_conn.connect()
            await vector_conn.connect()
            await _run_reqres(inf_conn, retrain_conn, vector_conn)

        elif DATAPLANE_BUS_MODE == "both":
            pubsub_conn = Connection(DATAPLANE_BUS_HOST, DATAPLANE_BUS_PORT)
            inf_conn = Connection(DATAPLANE_BUS_HOST, DATAPLANE_BUS_PORT)
            retrain_conn = Connection(DATAPLANE_BUS_HOST, DATAPLANE_BUS_PORT)
            vector_conn = Connection(DATAPLANE_BUS_HOST, DATAPLANE_BUS_PORT)
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
            log.error(
                "Unknown DATAPLANE_BUS_MODE=%r — use pubsub | reqres | both", DATAPLANE_BUS_MODE
            )

    async def _run_bridge_safe() -> None:
        """Run bridge with auto-reconnect; keeps status server alive on failures."""
        backoff = 2.0
        attempt = 0
        while True:
            attempt += 1
            try:
                if attempt > 1:
                    log.info("Reconnecting to Dataplane bus (attempt %d) …", attempt)
                await _run_bridge()
                log.info("Bridge exited cleanly")
                break
            except EOFError as exc:
                log.warning(
                    "Dataplane bus connection closed: %s — reconnecting in %.0fs", exc, backoff
                )
            except Exception:
                log.exception("Bridge connection failed — reconnecting in %.0fs", backoff)
            _bridge_stats["bridge_error"] = True
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    await asyncio.gather(run_status_server(), _run_bridge_safe())


if __name__ == "__main__":
    asyncio.run(capnp.run(main()))
