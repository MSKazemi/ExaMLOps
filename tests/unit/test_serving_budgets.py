"""Time and retry budgets on the inference hot path (plan P4.6).

A request's deadline is fixed once at the ingress and every hop spends from it: the router never
retries past it, the model server never runs a model for a caller who has gone, and a hung
pipeline is answered at the deadline rather than after each hop's own fixed timeout. Retries are
limited by a gRPC-style retry budget so an outage is not amplified, and overload is answered 503
with ``Retry-After`` instead of a growing queue.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from serving import budgets  # noqa: E402
from serving.budgets import HEADER, PAYLOAD_KEY, Deadline, RetryBudget  # noqa: E402
from serving.inference_pipeline import app as pipeline  # noqa: E402

# ─── Deadline ─────────────────────────────────────────────────────────────────


def test_a_received_budget_becomes_a_local_deadline():
    remaining = Deadline.from_budget_ms("500").remaining()
    assert 0.4 < remaining <= 0.5


@pytest.mark.parametrize("raw", [None, "", "soon", "nan"])
def test_a_missing_or_malformed_budget_gets_the_default_never_no_deadline(raw):
    remaining = Deadline.from_budget_ms(raw, default=7.0).remaining()
    assert 6.5 < remaining <= 7.0


def test_a_huge_budget_is_clamped():
    assert Deadline.from_budget_ms(str(10**9), cap=2.0).remaining() <= 2.0


@pytest.mark.parametrize("raw", ["0", "-40"])
def test_a_spent_budget_is_already_expired(raw):
    deadline = Deadline.from_budget_ms(raw)
    assert deadline.expired() and deadline.remaining() == 0.0
    assert deadline.budget_ms() == "0"


def test_the_budget_passed_on_is_what_is_left():
    deadline = Deadline.after(1.0)
    time.sleep(0.05)
    assert 900 <= int(deadline.budget_ms()) <= 955


# ─── RetryBudget ──────────────────────────────────────────────────────────────


def test_retries_stop_once_most_recent_calls_fail():
    budget = RetryBudget(max_tokens=10, token_ratio=0.1)
    for _ in range(4):
        budget.record_failure()
    assert budget.can_retry()  # 6 > 5
    budget.record_failure()
    assert not budget.can_retry()  # 5 is not more than half


def test_retries_resume_as_successes_refill_the_budget():
    budget = RetryBudget(max_tokens=10, token_ratio=0.1)
    for _ in range(10):
        budget.record_failure()
    assert budget.tokens == 0.0
    for _ in range(50):
        budget.record_success()
    assert not budget.can_retry()  # exactly 5.0: not yet
    budget.record_success()
    assert budget.can_retry()
    for _ in range(1000):
        budget.record_success()
    assert budget.tokens == 10.0  # capped


def test_a_retry_budget_must_be_positive():
    with pytest.raises(ValueError):
        RetryBudget(max_tokens=0)


# ─── the router ───────────────────────────────────────────────────────────────


# What the model server answers over Open Inference Protocol v2 (ADR 0126).
_OIP_OK = {
    "model_name": "jpcp",
    "model_version": "3",
    "parameters": {"alias": "Production", "run_id": "r3"},
    "outputs": [{"name": "predict", "datatype": "FP64", "shape": [1], "data": [1.0]}],
}


class _Server:
    """A scripted model server: each call pops the next response (or exception)."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        if isinstance(step, httpx.Response):
            return step
        return httpx.Response(step, json=_OIP_OK if step == 200 else {})


def _replica_died() -> httpx.Response:
    """What Ray's proxy answers when the replica dies with the request on it (Ray 2.55)."""
    return httpx.Response(
        500, text="Internal Server Error", headers={"content-type": "text/plain; charset=utf-8"}
    )


@pytest.fixture()
def router(monkeypatch):
    def install(server: _Server, retry_budget: RetryBudget | None = None) -> _Server:
        client = httpx.AsyncClient(transport=httpx.MockTransport(server))
        monkeypatch.setattr(pipeline, "_http_client", client)
        monkeypatch.setattr(pipeline, "_RETRY_BUDGET", retry_budget or RetryBudget())
        monkeypatch.setattr(pipeline, "_ROUTE_RETRIES", 2)
        return server

    return install


def _post(deadline: Deadline) -> dict:
    return asyncio.run(pipeline.ModelRouter._post("jpcp", "Production", {"x": 1}, deadline))


def test_a_transport_blip_is_retried_and_the_budget_travels(router):
    server = router(_Server(httpx.ConnectError("refused"), 200))
    assert _post(Deadline.after(5.0))["prediction"] == 1.0
    # The router speaks OIP v2 to the model server; /predict is deprecated.
    assert all(r.url.path == "/v2/models/jpcp/infer" for r in server.requests)
    assert len(server.requests) == 2
    sent = [int(r.headers[HEADER]) for r in server.requests]
    assert all(0 < ms <= 5000 for ms in sent) and sent[1] < sent[0]
    # Each attempt waits no longer than the request has left.
    assert all(r.extensions["timeout"]["read"] <= 5.0 for r in server.requests)


def test_an_overloaded_server_is_retried_then_reported_as_overload(router):
    server = router(_Server(503))
    assert _post(Deadline.after(5.0))["error"] == "overloaded"
    assert len(server.requests) == 3  # the ceiling: one attempt + INFERENCE_ROUTE_RETRIES


def test_a_spent_retry_budget_stops_retries_during_an_outage(router):
    spent = RetryBudget(max_tokens=10, token_ratio=0.1)
    for _ in range(5):
        spent.record_failure()
    server = router(_Server(503), retry_budget=spent)
    result = _post(Deadline.after(5.0))
    assert len(server.requests) == 1
    assert result["error"] == "overloaded"
    assert "retry budget spent" in result["detail"]


def test_no_retry_is_started_that_the_deadline_cannot_hold(router):
    server = router(_Server(httpx.ConnectError("refused")))
    _post(Deadline.after(0.12))  # first backoff is 0.1 s + the 0.05 s minimum attempt
    assert len(server.requests) == 1


def test_an_expired_request_is_never_sent(router):
    server = router(_Server(200))
    result = _post(Deadline.after(0.0))
    assert result["error"] == "deadline_exceeded" and not server.requests


def test_a_slow_model_is_not_retried(router):
    server = router(_Server(504))
    assert _post(Deadline.after(5.0))["error"] == "inference_failed"
    assert len(server.requests) == 1


def test_a_replica_lost_mid_request_is_retried_once(router):
    server = router(_Server(_replica_died(), 200))
    assert _post(Deadline.after(5.0))["prediction"] == 1.0
    assert len(server.requests) == 2


def test_a_second_lost_replica_is_not_retried_again(router):
    """The request may be what kills replicas: a query of death must not take a third one."""
    server = router(_Server(_replica_died()))
    assert _post(Deadline.after(5.0))["error"] == "inference_failed"
    assert len(server.requests) == 2


def test_the_model_servers_own_500_is_not_retried(router):
    server = router(_Server(httpx.Response(500, json={"error": "model raised ValueError"})))
    assert _post(Deadline.after(5.0))["error"] == "inference_failed"
    assert len(server.requests) == 1


def test_a_missing_model_is_definitive(router):
    server = router(_Server(404))
    assert _post(Deadline.after(5.0))["error"] == "model_not_found"
    assert len(server.requests) == 1


def test_route_takes_its_deadline_from_the_payload(router, monkeypatch):
    server = router(_Server(200))
    monkeypatch.setattr(pipeline, "_get_split", lambda _m: None)
    payload = {"model_name": "JPCP", "features": {"x": 1}, PAYLOAD_KEY: "800"}
    answer = asyncio.run(pipeline.ModelRouter().route(payload))
    assert answer == {
        "model_name": "jpcp",
        "model_version": "3",
        "alias": "Production",
        "run_id": "r3",
        "prediction": 1.0,
    }
    assert int(server.requests[0].headers[HEADER]) <= 800


# ─── transformer and ingress ──────────────────────────────────────────────────


def test_the_transformer_carries_the_budget():
    req = {"embedding": [0.0] * 384, "num_nodes": 1, PAYLOAD_KEY: "1234"}
    assert pipeline.FeatureTransformer._transform_one(req)[PAYLOAD_KEY] == "1234"
    del req[PAYLOAD_KEY]
    assert PAYLOAD_KEY not in pipeline.FeatureTransformer._transform_one(req)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        ("overloaded", 503),
        ("deadline_exceeded", 504),
        ("model_not_found", 404),
        ("validation_error", 422),
        ("inference_failed", 500),
    ],
)
def test_pipeline_errors_map_to_http_statuses(error, status):
    response = pipeline._pipeline_response({"error": error})
    assert response.status_code == status
    assert (response.headers.get("retry-after") == "1") is (status == 503)


def test_a_hung_pipeline_is_answered_at_the_deadline(monkeypatch):
    monkeypatch.setattr(pipeline, "_validate_payload", lambda _b: (True, []))
    monkeypatch.setattr(pipeline, "_DEADLINE_GRACE_SECONDS", 0.05)
    seen: dict = {}

    class _Transformer:
        class handle_batch:  # noqa: N801 - mirrors the Ray handle attribute
            @staticmethod
            def remote(body):
                seen.update(body)
                return asyncio.sleep(30)

    ingress = pipeline.InferencePipelineIngress(_Transformer())
    started = time.monotonic()
    response = asyncio.run(ingress.infer({"model_name": "JPCP"}, budget_ms="100"))
    assert response.status_code == 504
    assert time.monotonic() - started < 1.0
    assert 0 < int(seen[PAYLOAD_KEY]) <= 100


# ─── the model server ─────────────────────────────────────────────────────────


@pytest.fixture()
def server():
    from serving.ray_serving import app as rs_app

    srv = object.__new__(rs_app.MultiModelServer.func_or_class)
    srv._cache_lock = threading.RLock()
    srv._version_cache = OrderedDict()
    srv._version_cache_size = 8
    srv._preload_aliases = list(rs_app.PRELOAD_ALIASES)
    for attr in ("_req_counter", "_latency_hist", "_pred_value_hist", "_version_gauge"):
        setattr(srv, attr, MagicMock())
    from concurrent.futures import ThreadPoolExecutor

    srv._predict_pool = ThreadPoolExecutor(max_workers=1)
    srv._predict_timeout = 30.0
    srv._pool_lock = threading.Lock()
    srv._leaked_predicts = 0
    srv._pool_recycles = 0
    srv._pool_workers = 1
    yield srv, rs_app
    srv._predict_pool.shutdown(wait=False, cancel_futures=True)


def _statuses(srv) -> list[str]:
    return [c.kwargs["tags"]["status"] for c in srv._req_counter.inc.call_args_list]


def test_a_request_that_expired_in_the_queue_never_runs_the_model(server):
    srv, rs_app = server
    model = MagicMock()
    srv._hot = {("m", "Production"): {"model": model, "version": "1", "run_id": "r"}}
    request = rs_app.PredictRequest(features={"x": 1.0}, alias="Production")
    with pytest.raises(rs_app.HTTPException) as exc:
        srv.predict("m", request, budget_ms="0")
    assert exc.value.status_code == 504
    model.predict.assert_not_called()
    assert _statuses(srv) == ["deadline_exceeded"]


def test_the_model_runs_under_the_callers_budget_not_the_replica_limit(server):
    srv, rs_app = server

    class _Slow:
        def predict(self, _x):
            time.sleep(1.0)
            return [1.0]

    srv._hot = {("m", "Production"): {"model": _Slow(), "version": "1", "run_id": "r"}}
    pool = srv._predict_pool
    request = rs_app.PredictRequest(features={"x": 1.0}, alias="Production")
    started = time.monotonic()
    with pytest.raises(rs_app.HTTPException) as exc:
        srv.predict("m", request, budget_ms="100")
    assert exc.value.status_code == 504
    assert time.monotonic() - started < 0.8  # the replica's own limit is 30 s
    # The caller gave up; the thread is merely slow, not hung — the pool is not poisoned.
    assert srv._leaked_predicts == 0
    assert srv._pool_recycles == 0 and srv._predict_pool is pool
    assert _statuses(srv) == ["deadline_exceeded"]


# ─── load shedding ────────────────────────────────────────────────────────────


def test_queue_bounds_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("INFERENCE_MAX_QUEUED_REQUESTS", "64")
    reloaded = importlib.reload(pipeline)
    try:
        assert reloaded._IngressDeployment.max_queued_requests == 64
    finally:
        monkeypatch.delenv("INFERENCE_MAX_QUEUED_REQUESTS")
        importlib.reload(pipeline)
    assert pipeline._IngressDeployment.max_queued_requests == -1  # Ray's unbounded default

    from serving.ray_serving import app as rs_app

    source = Path(rs_app.__file__).read_text(encoding="utf-8")
    assert 'max_queued_requests=int(os.getenv("RAY_MAX_QUEUED_REQUESTS", "-1"))' in source


def test_the_budget_module_has_sane_defaults():
    assert budgets.DEFAULT_SECONDS == 30.0 and budgets.MAX_SECONDS == 300.0


def test_a_request_that_does_not_fit_the_model_is_a_validation_error(router):
    """OIP v2 answers 400 with the reason before the model runs; the pipeline reports it as the
    validation error it is (422 to the client), not an inference failure, and never retries it."""

    def refuse(request: httpx.Request) -> httpx.Response:
        refuse.calls += 1
        return httpx.Response(400, json={"error": "the model takes 3 features per row"})

    refuse.calls = 0
    router(refuse)
    result = _post(Deadline.after(5.0))
    assert result == {"error": "validation_error", "detail": "the model takes 3 features per row"}
    assert refuse.calls == 1


# ─── what the router records (plan P4.6 follow-up) ──────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.retries: list[str] = []

    def record_request(self, model_name: str, outcome: str) -> None:
        self.requests.append((model_name, outcome))

    def record_retry(self, reason: str) -> None:
        self.retries.append(reason)


@pytest.fixture()
def recorded(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(pipeline, "_router_metrics", lambda: recorder)
    return recorder


@pytest.mark.parametrize(
    ("script", "retries", "outcome"),
    [
        ((httpx.ConnectError("refused"), 200), ["transport"], "success"),
        ((503,), ["overloaded", "overloaded"], "overloaded"),
        ((_replica_died(), 200), ["replica_lost"], "success"),
        ((404,), [], "model_not_found"),
        ((504,), [], "inference_failed"),
    ],
)
def test_each_request_and_retry_is_counted(router, recorded, script, retries, outcome):
    router(_Server(*script))
    _post(Deadline.after(5.0))
    assert recorded.retries == retries
    assert recorded.requests == [("jpcp", outcome)]


def test_a_retry_the_budget_refuses_is_counted_as_such(router, recorded):
    spent = RetryBudget(max_tokens=10, token_ratio=0.1)
    for _ in range(5):
        spent.record_failure()
    router(_Server(503), retry_budget=spent)
    _post(Deadline.after(5.0))
    assert recorded.retries == ["budget_spent"]


def test_outside_ray_the_metrics_are_a_no_op(monkeypatch):
    monkeypatch.setattr(pipeline, "_metrics", None)
    metrics = pipeline._router_metrics()
    assert isinstance(metrics, pipeline._NoMetrics)
    assert metrics.record_request("jpcp", "success") is None  # no Ray process, nothing raised
    assert metrics.record_retry("transport") is None
    assert pipeline._metrics is None  # decided again once Ray is up, not cached as a no-op


# ── Why an inference failed (``cause``): only the model's own failure is about the model ─────


@pytest.mark.parametrize(
    ("script", "cause"),
    [
        ((httpx.Response(500, json={"error": "model raised ValueError"}),), "model"),
        ((504,), "timeout"),  # the model server's own deadline: slow, not wrong
        ((httpx.ConnectError("refused"),), "transport"),
        ((httpx.ReadTimeout("no answer"),), "transport"),  # timed out, deadline not yet spent
        ((_replica_died(),), "replica_lost"),
        ((httpx.Response(200, json={"not": "an OIP answer"}),), "protocol"),
    ],
)
def test_an_inference_failure_says_why(router, script, cause):
    """Drift and retrain triggers count only `model`: an outage or a lost replica is not a
    worse model (examlops-31's dataplane feeds drift only on cause `model`)."""
    router(_Server(*script))
    result = _post(Deadline.after(5.0))
    assert result["error"] == "inference_failed"
    assert result["cause"] == cause, result


def test_a_spent_retry_budget_keeps_the_underlying_cause(router):
    spent = RetryBudget(max_tokens=10, token_ratio=0.1)
    for _ in range(5):
        spent.record_failure()
    router(_Server(httpx.ConnectError("refused")), retry_budget=spent)
    result = _post(Deadline.after(5.0))
    assert result["cause"] == "transport" and "retry budget spent" in result["detail"]


def test_a_router_that_fails_is_a_pipeline_cause(monkeypatch):
    """The router actor itself failing is the pipeline's fault, never the model's."""

    class _Remote:
        async def remote(self, _payload):
            raise RuntimeError("actor died")

    class _Router:
        route = _Remote()

    monkeypatch.setattr(
        pipeline.FeatureTransformer, "_transform_one", staticmethod(lambda req: dict(req))
    )
    transformer = object.__new__(pipeline.FeatureTransformer)
    transformer._router = _Router()
    request = {"model_name": "jpcp", "alias": "Production", "features": {"x": 1}}
    batch = pipeline.FeatureTransformer.handle_batch
    handle = getattr(batch, "__wrapped__", batch)
    (result,) = asyncio.run(handle(transformer, [request]))
    assert result["error"] == "inference_failed" and result["cause"] == "pipeline"


def test_the_default_budget_retries_a_lost_replicas_whole_burst(monkeypatch):
    """A replica that dies with its requests fails them all at once. At 10 tokens only about 5
    were retried and the rest failed (the live failover test lost 1 of 320 that way); the budget
    is there to stop a sustained outage being multiplied, not to fail a one-off replica loss."""
    monkeypatch.delenv("INFERENCE_RETRY_MAX_TOKENS", raising=False)
    monkeypatch.delenv("INFERENCE_RETRY_TOKEN_RATIO", raising=False)
    budget = RetryBudget.from_env()
    burst = 0
    while budget.can_retry():
        budget.record_failure()
        burst += 1
    assert burst >= 40  # a replica's worth of in-flight requests, each retried once
    assert burst <= 60  # and still a bound: a real outage stops being retried soon
    # After the burst, retries are earned back at about one per ten successes.
    retries = 0
    for _ in range(100):
        budget.record_success()
        if budget.can_retry():
            budget.record_failure()  # the retry fails too: a sustained outage
            retries += 1
    assert 8 <= retries <= 12, retries
