# serving/inference_pipeline/app.py
import asyncio
import logging
import os
import random
import threading
import time
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header
from fastapi.responses import JSONResponse
from opentelemetry import trace as _otel_trace
from ray import serve

from examlops import oip_client
from examlops.observability import setup_tracing
from serving.admin_auth import require_serving_admin
from serving.budgets import HEADER as _BUDGET_HEADER
from serving.budgets import PAYLOAD_KEY as _BUDGET_KEY
from serving.budgets import Deadline, RetryBudget

try:
    from examlops.platform_db import get_traffic_rules as _db_get_traffic
    from examlops.platform_db import set_traffic_rules as _db_set_traffic
    from examlops.resilience import httpx_timeout as _httpx_timeout
except Exception:  # pragma: no cover - examlops always present in the serving image
    _db_get_traffic = _db_set_traffic = None  # type: ignore[assignment]

    def _httpx_timeout(read=None, connect=None):  # type: ignore[misc]
        return httpx.Timeout(10.0)


_log = logging.getLogger("inference_pipeline")

_DEFAULT_MODEL = os.getenv("DATAPLANE_BUS_DEFAULT_MODEL", "JPCP")
_DEFAULT_ALIAS = os.getenv("DATAPLANE_BUS_DEFAULT_ALIAS", "Production")
_RAY_SERVE_URL = os.getenv("RAY_SERVE_URL", "http://localhost:8001").rstrip("/")
# Transient-error retries for the ingress→MultiModelServer hop — a ceiling. Each retry must also
# fit the request's remaining deadline and be allowed by the process-wide retry budget (P4.6).
_ROUTE_RETRIES = int(os.getenv("INFERENCE_ROUTE_RETRIES", "2"))
_RETRY_BUDGET = RetryBudget.from_env()
# Below this much remaining budget another attempt cannot plausibly finish; stop instead.
_MIN_ATTEMPT_SECONDS = 0.05
# How long past the deadline the ingress waits for the pipeline's own, more specific answer.
_DEADLINE_GRACE_SECONDS = 0.5

# The router and the ingress are separate Serve actors (separate processes), and the
# `exa serve traffic` CLI writes splits from yet another process — so the cache must expire,
# or a split change (e.g. a canary rollback to --production 100) silently never applies to a
# router that already cached the old rules.
_TRAFFIC_TTL_SECONDS = float(os.getenv("TRAFFIC_RULES_TTL_SECONDS", "30"))
# model → (rules or None, monotonic expiry). Negative results are cached too, so a model with
# no configured split doesn't pay a DB read on every request.
_traffic_rules: dict[str, tuple[dict[str, int] | None, float]] = {}
_traffic_lock = threading.Lock()


# Serving snapshot (ADR 0127): with one in force the router takes splits from it — the same
# generation every serving replica acts on — instead of its own TTL read of the table.
_SNAPSHOT_MODE = os.getenv("RAY_SNAPSHOT_MODE", "auto").strip().lower()
_SNAPSHOT_POLL_SECONDS = max(0.5, float(os.getenv("RAY_SNAPSHOT_POLL_SECONDS", "2")))
_snapshot_state: dict[str, Any] = {"reader": None, "snapshot": None, "next": 0.0}
_snapshot_lock = threading.Lock()


def _snapshot() -> dict[str, Any] | None:
    """The serving snapshot in force for this router, refreshed at most every poll interval."""
    if _SNAPSHOT_MODE == "off":
        return None
    now = time.monotonic()
    with _snapshot_lock:
        if now < _snapshot_state["next"]:
            return _snapshot_state["snapshot"]
        _snapshot_state["next"] = now + _SNAPSHOT_POLL_SECONDS
        reader = _snapshot_state["reader"]
    try:
        if reader is None:
            from serving.ray_serving.snapshot import SnapshotReader  # noqa: PLC0415

            reader = SnapshotReader()
        current = reader.newest()
    except Exception as exc:  # noqa: BLE001 - no snapshot means the table path below
        _log.debug("serving snapshot unavailable to the router: %s", exc)
        current = None
    with _snapshot_lock:
        _snapshot_state["reader"] = reader
        _snapshot_state["snapshot"] = current
    return current


def _get_split(model_name: str) -> dict[str, int] | None:
    """Return the traffic split for *model_name*.

    From the serving snapshot when one is in force; otherwise as below.

    Short-TTL per-replica cache over the durable ``platform_db`` copy, so rules survive replica
    restarts, stay consistent across replicas and with the ``exa serve traffic`` CLI, and
    cross-process changes apply within ``TRAFFIC_RULES_TTL_SECONDS``. DB errors degrade to
    "no split" rather than failing inference.
    """
    snapshot = _snapshot()
    if snapshot is not None:
        entry = snapshot.get("traffic", {}).get(model_name.strip().lower())
        return dict(entry["rules"]) if entry and entry.get("rules") else None
    now = time.monotonic()
    with _traffic_lock:
        hit = _traffic_rules.get(model_name)
        if hit is not None and hit[1] > now:
            return hit[0]
    if _db_get_traffic is None:
        return None
    try:
        rules = _db_get_traffic(model_name) or None
    except Exception as exc:  # noqa: BLE001
        _log.warning("traffic-rule DB read failed for %s: %s", model_name, exc)
        return None
    with _traffic_lock:
        _traffic_rules[model_name] = (rules, now + _TRAFFIC_TTL_SECONDS)
    return rules


# One long-lived HTTP client for the router→model-server hop: a new AsyncClient per request
# pays TCP + pool construction on the inference hot path, twice per bus job end-to-end.
_http_client: httpx.AsyncClient | None = None
_http_client_lock = threading.Lock()


def _shared_http_client() -> httpx.AsyncClient:
    global _http_client
    with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.AsyncClient(timeout=_httpx_timeout())
        return _http_client


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
        # A traffic split redistributes the traffic addressed to the model's *default* alias —
        # the endpoint a canary is meant to shadow. A request pinned to any other alias (an
        # operator testing Staging, an explicit rollback probe) gets exactly what it asked for.
        # Before P0.4 a configured split overrode every alias, so `--alias Staging` could be
        # answered by Production. The bus bridge always sends the default alias, so bus traffic
        # still follows the split.
        if requested_alias.lower() != _DEFAULT_ALIAS.lower():
            return model_name, requested_alias
        split = _get_split(model_name)
        if split and len(split) > 1:
            aliases = list(split.keys())
            weights = [split[a] for a in aliases]
            chosen = random.choices(aliases, weights=weights, k=1)[0]
            return model_name, chosen
        return model_name, requested_alias

    async def route(self, payload: dict[str, Any]) -> dict[str, Any]:
        deadline = Deadline.from_budget_ms(payload.get(_BUDGET_KEY))
        # _resolve may read platform_db on a cache miss — a blocking sqlite call that must not
        # run on this single-replica actor's event loop (the QW10 rule).
        model_name, alias = await asyncio.to_thread(self._resolve, payload)
        features = payload["features"]
        with _tracer.start_as_current_span("inference_pipeline.model_router") as span:
            span.set_attribute("model_name", model_name)
            span.set_attribute("alias", alias)
            return await self._post(model_name, alias, features, deadline)

    @staticmethod
    async def _post(
        model_name: str, alias: str, features: Any, deadline: Deadline
    ) -> dict[str, Any]:
        """Route one request to the model server and count how it ended (``_attempts``)."""
        result = await ModelRouter._attempts(model_name, alias, features, deadline)
        _router_metrics().record_request(model_name, result.get("error", "success"))
        return result

    @staticmethod
    async def _attempts(
        model_name: str, alias: str, features: Any, deadline: Deadline
    ) -> dict[str, Any]:
        """POST to the model server within ``deadline``, retrying only what is worth retrying.

        Retried: transport errors, 503 (a replica shedding load or not yet ready) and, once, a
        replica lost mid-request. Not retried: every other status — a 4xx will not change, a 504
        means the model itself was too slow, which a second attempt only doubles, and the model
        server's own 500 is the model failing. A retry happens only while the deadline leaves room
        for it and the process-wide retry budget allows it, so an outage is not amplified.
        """
        overloaded = {"error": "overloaded", "model_name": model_name, "alias": alias}
        last: dict[str, Any] = _inference_failed("no attempt made", "pipeline")
        lost_once = False
        reason = "transport"  # why the attempt failed, for the retry counter
        for attempt in range(_ROUTE_RETRIES + 1):
            if deadline.expired():
                return _deadline_exceeded(model_name, alias)
            remaining = deadline.remaining()
            try:
                # Open Inference Protocol v2 (ADR 0126); /predict is deprecated.
                resp = await _shared_http_client().post(
                    f"{_RAY_SERVE_URL}{oip_client.infer_path(model_name)}",
                    json=oip_client.features_request(features, alias=alias),
                    headers={_BUDGET_HEADER: deadline.budget_ms()},
                    timeout=httpx.Timeout(remaining, connect=min(remaining, 5.0)),
                )
            except httpx.TimeoutException as exc:
                _RETRY_BUDGET.record_failure()
                if deadline.expired():
                    return _deadline_exceeded(model_name, alias)
                last, reason = _inference_failed(str(exc), "transport"), "transport"
            except httpx.RequestError as exc:
                _RETRY_BUDGET.record_failure()
                last, reason = _inference_failed(str(exc), "transport"), "transport"
            except Exception as exc:  # noqa: BLE001
                return _inference_failed(str(exc), "pipeline")
            else:
                if resp.status_code == 503:
                    _RETRY_BUDGET.record_failure()
                    last, reason = overloaded, "overloaded"
                elif _replica_lost(resp):
                    # Once, not twice: the request may be what killed the replica, and a query
                    # of death retried twice takes a third replica down with it.
                    _RETRY_BUDGET.record_failure()
                    last = _inference_failed(
                        "the serving replica was lost during the request", "replica_lost"
                    )
                    if lost_once:
                        return last
                    lost_once, reason = True, "replica_lost"
                else:
                    _RETRY_BUDGET.record_success()
                    if resp.status_code == 404:
                        return {
                            "error": "model_not_found",
                            "model_name": model_name,
                            "alias": alias,
                        }
                    if resp.status_code == 400:  # the request does not fit the model's signature
                        return {"error": "validation_error", "detail": _oip_error(resp)}
                    if resp.status_code == 504 and deadline.expired():
                        return _deadline_exceeded(model_name, alias)
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        # 504: the model ran out of its own time (slow, not wrong). Any other
                        # error status is the model server failing this request: the model.
                        cause = "timeout" if resp.status_code == 504 else "model"
                        return _inference_failed(str(exc), cause)
                    try:
                        return oip_client.result(resp.json())
                    except Exception as exc:  # noqa: BLE001
                        return _inference_failed(str(exc), "protocol")
            if attempt >= _ROUTE_RETRIES:
                break
            backoff = 0.1 * (2**attempt)
            if deadline.remaining() <= backoff + _MIN_ATTEMPT_SECONDS:
                break
            if not _RETRY_BUDGET.can_retry():
                _router_metrics().record_retry("budget_spent")
                last = {
                    **last,
                    "detail": f"{last.get('detail', last['error'])}; retry budget spent",
                }
                break
            _router_metrics().record_retry(reason)
            await asyncio.sleep(backoff)
        return last


class _RouterMetrics:
    """What the router did, for Prometheus (Ray metrics, so the ``ray_`` prefix is added).

    ``examlops_router_requests_total{model_name, outcome}``: one per routed request, by how it
    ended (``success`` or the pipeline's error code). ``examlops_router_retries_total{reason}``:
    one per retry the router made (``transport``, ``overloaded``, ``replica_lost``) or refused
    because the retry budget was spent (``budget_spent``). A lost replica and a spent budget are
    otherwise visible only in Ray's logs.
    """

    def __init__(self) -> None:
        from ray.util.metrics import Counter  # noqa: PLC0415 - only inside a Ray process

        self._requests = Counter(
            "examlops_router_requests_total",
            description="Requests the inference router routed, by final outcome",
            tag_keys=("model_name", "outcome"),
        )
        self._retries = Counter(
            "examlops_router_retries_total",
            description="Retries the inference router made, or refused (budget_spent), by reason",
            tag_keys=("reason",),
        )

    def record_request(self, model_name: str, outcome: str) -> None:
        self._requests.inc(tags={"model_name": model_name, "outcome": outcome})

    def record_retry(self, reason: str) -> None:
        self._retries.inc(tags={"reason": reason})


class _NoMetrics:
    """Outside a Ray process (unit tests, tools importing the module) there is nothing to export."""

    def record_request(self, model_name: str, outcome: str) -> None:
        pass

    def record_retry(self, reason: str) -> None:
        pass


_metrics: _RouterMetrics | _NoMetrics | None = None


def _router_metrics() -> _RouterMetrics | _NoMetrics:
    global _metrics
    if _metrics is None:
        try:
            import ray  # noqa: PLC0415

            if not ray.is_initialized():
                return _NoMetrics()  # decided again once Ray is up
            _metrics = _RouterMetrics()
        except Exception:  # noqa: BLE001 - metrics must never fail a request
            _metrics = _NoMetrics()
    return _metrics


#: Why an inference failed. Only ``model`` is evidence about the model: drift trackers and retrain
#: triggers count that one (or an answer without a cause, from an older pipeline) and nothing else.
INFERENCE_FAILURE_CAUSES = ("model", "timeout", "transport", "replica_lost", "protocol", "pipeline")


def _inference_failed(detail: str, cause: str) -> dict[str, Any]:
    """The pipeline's answer when no prediction came back, with why (``cause``).

    ``model``: the model server answered this request with an error (the model failed on it).
    ``timeout``: the model server's own deadline ran out (slow, not wrong). ``transport``: no answer
    reached the pipeline after its retries. ``replica_lost``: the serving replica died with the
    request on it. ``protocol``: a success that is not an inference answer. ``pipeline``: the
    pipeline's own router failed.
    """
    assert cause in INFERENCE_FAILURE_CAUSES, cause
    return {"error": "inference_failed", "detail": detail, "cause": cause}


def _replica_lost(resp: httpx.Response) -> bool:
    """Whether a 500 is Ray's proxy reporting a replica that died with the request on it.

    The model server answers its own errors in JSON (``{"error": …}``); when a replica dies
    mid-request Ray's proxy has nothing to relay and its HTTP server sends a plain-text
    ``Internal Server Error`` instead. Nothing else tells the two apart on the wire.
    """
    content_type = resp.headers.get("content-type", "")
    return resp.status_code == 500 and not content_type.startswith("application/json")


def _oip_error(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error") or resp.text)
    except ValueError:
        return resp.text


def _deadline_exceeded(model_name: str, alias: str) -> dict[str, Any]:
    return {"error": "deadline_exceeded", "model_name": model_name, "alias": alias}


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
        transformed = {
            "model_name": req.get("model_name"),
            "alias": req.get("alias"),
            "job_id": req.get("job_id"),
            "num_nodes": int(num_nodes),
            "user_id": str(req.get("user_id", "")),
            "features": {
                "embedding": list(embedding),
            },
        }
        if req.get(_BUDGET_KEY) is not None:
            transformed[_BUDGET_KEY] = req[_BUDGET_KEY]
        return transformed

    @serve.batch(max_batch_size=32, batch_wait_timeout_s=0.05)
    async def handle_batch(self, reqs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any] | None] = [None] * len(reqs)
        valid: list[tuple[int, dict[str, Any]]] = []

        for i, req in enumerate(reqs):
            try:
                transformed = self._transform_one(req)
            except ValueError as exc:
                results[i] = {"error": "validation_error", "detail": str(exc)}
                continue
            # Re-stamp the budget after the batching wait, and drop what nobody is waiting for.
            deadline = Deadline.from_budget_ms(transformed.get(_BUDGET_KEY))
            if deadline.expired():
                results[i] = _deadline_exceeded(
                    str(transformed.get("model_name") or ""), str(transformed.get("alias") or "")
                )
                continue
            transformed[_BUDGET_KEY] = deadline.budget_ms()
            valid.append((i, transformed))

        if valid:
            router_results = await asyncio.gather(
                *[self._router.route.remote(payload) for _, payload in valid],
                return_exceptions=True,
            )
            for (idx, _), result in zip(valid, router_results):
                if isinstance(result, BaseException):
                    results[idx] = _inference_failed(str(result), "pipeline")
                else:
                    results[idx] = result

        return results  # type: ignore[return-value]


# ── A5 inference gate (ADR 0005 clause 2) ─────────────────────────────────────
#
# The contract layer shipped with a `validate_request` written for exactly this ingress — its
# docstring says "so the ingress can return a 4xx instead of a 5xx" — and nothing outside its own
# tests called it, while the ingress hand-rolled a two-field presence check. Two validators, one
# of which knew about embedding dimensionality and was never asked.

#: Fields every inference request must carry. Kept here rather than derived from a dataset
#: contract: a `DataContract` describes training **columns**, and a request is not a row of the
#: training table — deriving one from the other would be a guess wearing a contract's name.
_REQUIRED_FIELDS = ("embedding", "num_nodes")


def _embedding_dim() -> int | None:
    """Declared embedding width, or None. Never defaulted — 384 is a fact about a use case."""
    raw = os.environ.get("EXAMLOPS_INFERENCE_EMBEDDING_DIM", "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        _log.warning("EXAMLOPS_INFERENCE_EMBEDDING_DIM=%r is not an integer; ignoring", raw)
        return None


def _validate_payload(body: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate one inference request through the A5 contract layer (R8/R9).

    Degrades to the previous presence check if the contract package is not importable in this
    process — a serving replica that cannot import `pipelines` must still serve, and refusing
    every request because a *validator* is missing would be a far worse failure than the one
    this gate prevents.
    """
    try:
        from pipelines.contracts import validate_request
    except Exception:  # noqa: BLE001 - a replica that cannot import the contract package must
        # still serve; the docstring above argues why refusing every request would be worse.
        missing = [f"missing required field '{f}'" for f in _REQUIRED_FIELDS if f not in body]
        return (not missing, missing)
    return validate_request(
        body,
        required=_REQUIRED_FIELDS,
        embedding_field="embedding",
        embedding_dim=_embedding_dim(),
    )


class InferencePipelineIngress:
    def __init__(self, transformer: Any) -> None:
        self._transformer = transformer

    @_ingress_app.get("/health")
    async def health(self) -> dict[str, Any]:
        """Liveness for the inference-pipeline ingress (process + loop responsive)."""
        return {"status": "alive", "ray_serve_url": _RAY_SERVE_URL}

    # Rerouting production traffic is an admin action (plan P0.6 / finding S1).
    @_ingress_app.post(
        "/traffic-rules/{model}", response_model=None, dependencies=[Depends(require_serving_admin)]
    )
    async def set_traffic(self, model: str, body: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        try:
            rules = {k: int(v) for k, v in body.items()}
        except (TypeError, ValueError) as exc:
            return JSONResponse(
                {"error": "validation_error", "detail": f"rule values must be integers: {exc}"},
                status_code=422,
            )
        with _traffic_lock:
            _traffic_rules[model.lower()] = (rules, time.monotonic() + _TRAFFIC_TTL_SECONDS)
        # Persist so the split survives replica restarts and is shared across replicas. Note the
        # in-memory copy above only covers THIS actor; the router actor picks the change up from
        # the DB within _TRAFFIC_TTL_SECONDS.
        if _db_set_traffic is not None:
            try:
                await asyncio.to_thread(
                    _db_set_traffic, model.lower(), rules, updated_by="inference_pipeline"
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("traffic-rule DB write failed for %s: %s", model, exc)
        return {"ok": True, "model": model, "rules": rules}

    @_ingress_app.get("/traffic-rules")
    async def get_traffic(self) -> dict[str, Any]:
        now = time.monotonic()
        with _traffic_lock:
            return {
                model: rules
                for model, (rules, expiry) in _traffic_rules.items()
                if rules is not None and expiry > now
            }

    @_ingress_app.post("/infer")
    async def infer(
        self,
        body: dict[str, Any],
        budget_ms: Annotated[str | None, Header(alias=_BUDGET_HEADER)] = None,
    ) -> Any:
        # The request's deadline is fixed here, once, and every hop below spends from it.
        deadline = Deadline.from_budget_ms(budget_ms)
        with _tracer.start_as_current_span("inference_pipeline.ingress") as span:
            span.set_attribute("model_name", body.get("model_name") or "")
            span.set_attribute("alias", body.get("alias") or "")
            ok, errors = _validate_payload(body)
            if not ok:
                return JSONResponse(
                    {"error": "validation_error", "detail": "; ".join(errors)},
                    status_code=422,
                )
            body[_BUDGET_KEY] = deadline.budget_ms()
            try:
                result = await asyncio.wait_for(
                    self._transformer.handle_batch.remote(body),
                    timeout=deadline.remaining() + _DEADLINE_GRACE_SECONDS,
                )
            except TimeoutError:
                result = _deadline_exceeded(
                    str(body.get("model_name") or ""), str(body.get("alias") or "")
                )
            return _pipeline_response(result)


#: How each pipeline error reaches the HTTP client.
_ERROR_STATUS = {
    "validation_error": 422,
    "model_not_found": 404,
    "overloaded": 503,
    "deadline_exceeded": 504,
}


def _pipeline_response(result: dict[str, Any]) -> Any:
    if "error" not in result:
        return result
    status = _ERROR_STATUS.get(result["error"], 500)
    # 503 is a promise the condition is temporary; say when to come back (RFC 9110 §10.2.3).
    headers = {"Retry-After": "1"} if status == 503 else None
    return JSONResponse(result, status_code=status, headers=headers)


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
# Two of each by default (plan P4.8): with one, a replica restart — a deploy, an OOM, a node drain —
# took the whole pipeline down until Ray brought it back. They hold no state a peer lacks (traffic
# splits and the serving snapshot are read from the shared store), so more is only redundancy.
_PIPELINE_REPLICAS = max(1, int(os.getenv("INFERENCE_PIPELINE_REPLICAS", "2")))
_ModelRouterDeployment = serve.deployment(
    num_replicas=_PIPELINE_REPLICAS, ray_actor_options=_PIPELINE_ACTOR_OPTS
)(ModelRouter)
_FeatureTransformerDeployment = serve.deployment(
    num_replicas=_PIPELINE_REPLICAS, ray_actor_options=_PIPELINE_ACTOR_OPTS
)(FeatureTransformer)
# Load shedding at the front door (P4.6): past this many requests queued at the HTTP proxy, Ray
# Serve answers 503 at once instead of letting the queue — and every caller's latency — grow.
# -1 (Ray's default) is unbounded; the right bound depends on replica count and model latency,
# so a site sets it (docs/components/ray-serve.md, "Overload").
_IngressDeployment = serve.deployment(
    num_replicas=_PIPELINE_REPLICAS,
    ray_actor_options=_PIPELINE_ACTOR_OPTS,
    max_queued_requests=int(os.getenv("INFERENCE_MAX_QUEUED_REQUESTS", "-1")),
)(serve.ingress(_ingress_app)(InferencePipelineIngress))

# Deployment graph — imported by serving/ray_serving/app.py
router = _ModelRouterDeployment.bind()
transformer = _FeatureTransformerDeployment.bind(router)
pipeline_app = _IngressDeployment.bind(transformer)
