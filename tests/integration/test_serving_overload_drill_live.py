"""Chaos drill: the model server is loaded past its capacity (plan P5).

A real Ray Serve model server from an image built from this tree, one replica, serving a real
sklearn model registered in a real MLflow, loaded far above what it can finish. The drill holds the
serving plane to what `docs/components/ray-serve.md` promises under overload:

- **shed, do not collapse.** With `RAY_MAX_QUEUED_REQUESTS` set, every answer is a prediction or a
  503 — never a 500, never a hung connection;
- **the bound is what protects latency.** The same load with the queue unbounded answers almost
  everything, but the requests that succeed wait far longer: the drill measures both and compares;
- **a spent budget is refused without running the model.** A request that arrives with no time left
  is answered 504, not run;
- **it recovers by itself.** After the burst the server is ready and still serving its model.

The inference pipeline's own shed answer (`overloaded` with `Retry-After: 1`) is a pure mapping and
is covered by `tests/unit/test_serving_budgets.py`; driving it here would need the use-case model's
real feature contract, which this drill deliberately does not depend on.

Opt-in and slow (Docker, an image build, and a real model load)::

    docker build -f serving/ray_serving/Dockerfile -t exa-chaos/ray-serving:tree .
    EXAMLOPS_CHAOS_LIVE=1 .venv/bin/pytest tests/integration/test_serving_overload_drill_live.py -v -s
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import statistics
import subprocess
import time
import uuid

import httpx
import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("EXAMLOPS_CHAOS_LIVE") != "1", reason="set EXAMLOPS_CHAOS_LIVE=1"),
]

IMAGE = "exa-chaos/ray-serving:tree"
QUEUE_BOUND = 4  # tiny on purpose: one replica, so the bound is reached at once
# Far above one replica's capacity: it answers this model in about 15 ms, so ~65 requests a second
# is *not* overload. The first run of this drill used 60/s and shed nothing, which proved nothing.
RATE = 600.0
DURATION = 6.0
MEASURED: dict[str, object] = {}

# Trains and registers a model with a signature, through the same MLflow the server reads.
REGISTER = """
import mlflow, numpy as np, pandas as pd
from mlflow.models import infer_signature
from sklearn.linear_model import LinearRegression
mlflow.set_tracking_uri("http://mlflow:5000")
X = pd.DataFrame(np.random.default_rng(0).random((20, 3)), columns=["cpu", "mem", "nodes"])
y = X["cpu"] + 2 * X["mem"] + 3 * X["nodes"]
with mlflow.start_run():
    mlflow.sklearn.log_model(LinearRegression().fit(X, y), name="model",
                             signature=infer_signature(X, y), registered_model_name="jpcp")
c = mlflow.MlflowClient()
version = c.get_latest_versions("jpcp")[0].version
c.set_model_version_tag("jpcp", version, "framework", "sklearn")
c.set_registered_model_alias("jpcp", "Production", version)
print("registered", version)
"""

INFER = {
    "inputs": [{"name": "input-0", "shape": [1, 3], "datatype": "FP64", "data": [1.0, 1.0, 1.0]}]
}


def _run(*args: str, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess:
    out = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    if check and out.returncode != 0:
        raise AssertionError(f"{' '.join(args[:6])}…: {out.stderr[-2000:]}")
    return out


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _until(fn, timeout: float, interval: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = fn()
        if found:
            return found
        time.sleep(interval)
    return None


def _serving(net: str, name: str, port: int, queue_bound: int) -> None:
    _run(
        "docker", "run", "-d", "--rm", "--name", name, "--network", net, "--shm-size", "1g",
        "-p", f"127.0.0.1:{port}:8001",
        "-e", "MLFLOW_TRACKING_URI=http://mlflow:5000",
        "-e", "RAY_NUM_REPLICAS=1", "-e", "RAY_SNAPSHOT_MODE=off", "-e", "OTEL_SDK_DISABLED=true",
        "-e", "RAY_PRELOAD_ALIASES=Production", "-e", f"RAY_MAX_QUEUED_REQUESTS={queue_bound}",
        "-e", "RAY_MAX_ONGOING_REQUESTS=2", "-e", "RAY_SERVE_GRPC_PORT=0",
        IMAGE,
    )  # fmt: skip


def _ready(base: str) -> bool:
    try:
        return httpx.get(f"{base}/models", timeout=5).json() != []
    except Exception:  # noqa: BLE001 - still starting
        return False


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    if _run("docker", "image", "inspect", IMAGE, check=False).returncode:
        pytest.skip(f"build {IMAGE} first (see the module docstring)")
    tag = uuid.uuid4().hex[:6]
    net = f"exa-load-{tag}"
    names = {k: f"exa-load-{k}-{tag}" for k in ("mlflow", "bounded", "unbounded")}
    tmp = tmp_path_factory.mktemp("load")
    tmp.chmod(0o755)
    bounded_port, unbounded_port = _port(), _port()
    try:
        _run("docker", "network", "create", net)
        _run(
            "docker", "run", "-d", "--rm", "--name", names["mlflow"], "--network", net,
            "--network-alias", "mlflow", "--entrypoint", "mlflow", IMAGE, "server",
            "--host", "0.0.0.0", "--port", "5000", "--backend-store-uri", "sqlite:////tmp/mlflow.db",
            "--serve-artifacts", "--artifacts-destination", "/tmp/artifacts",
            "--allowed-hosts", "mlflow,mlflow:5000,localhost,localhost:5000,127.0.0.1:5000",
        )  # fmt: skip
        assert _until(
            lambda: _run("docker", "exec", names["mlflow"], "python", "-c",
                         "import urllib.request;urllib.request.urlopen('http://localhost:5000/health')",
                         check=False).returncode == 0,
            timeout=120,
        ), "mlflow never came up"  # fmt: skip
        (tmp / "register.py").write_text(REGISTER)
        (tmp / "register.py").chmod(0o644)
        _run(
            "docker", "run", "--rm", "--network", net, "-v", f"{tmp}:/w:ro",
            "--entrypoint", "python", IMAGE, "/w/register.py",
        )  # fmt: skip
        _serving(net, names["bounded"], bounded_port, QUEUE_BOUND)
        _serving(net, names["unbounded"], unbounded_port, -1)
        bounded = f"http://127.0.0.1:{bounded_port}"
        unbounded = f"http://127.0.0.1:{unbounded_port}"
        for base in (bounded, unbounded):
            assert _until(lambda: _ready(base), timeout=300), f"{base} never loaded the model"
        yield {"bounded": bounded, "unbounded": unbounded, "names": names}
    finally:
        for name in names.values():
            _run("docker", "rm", "-f", name, check=False)
        _run("docker", "network", "rm", net, check=False)
        if MEASURED:
            print("\noverload drill:", json.dumps(MEASURED, indent=1))


def _load(base: str, path: str, body: dict, *, rate: float = RATE, duration: float = DURATION):
    from examlops import loadtest

    return asyncio.run(loadtest.run(base + path, body, rate=rate, duration=duration, timeout=30.0))


def _p(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1) if ordered else -1.0


# ── the drill ─────────────────────────────────────────────────────────────────


def test_1_a_bounded_queue_sheds_instead_of_collapsing(stack):
    report = _load(stack["bounded"], "/v2/models/jpcp/infer", INFER)
    statuses = dict(report.statuses)
    MEASURED["bounded_statuses"] = statuses
    MEASURED["bounded_transport_errors"] = report.transport_errors
    MEASURED["bounded_p50_ms"] = _p(report.latencies_ms, 0.50)
    MEASURED["bounded_p99_ms"] = _p(report.latencies_ms, 0.99)
    assert report.sent > 0 and report.succeeded > 0, statuses
    assert set(statuses) <= {200, 503}, f"an overloaded server answered something else: {statuses}"
    assert statuses.get(503, 0) > 0, "the queue bound shed nothing; is the load above capacity?"
    assert report.transport_errors == 0, "connections were dropped rather than answered"


def test_2_the_bound_is_what_keeps_latency_down(stack):
    report = _load(stack["unbounded"], "/v2/models/jpcp/infer", INFER)
    MEASURED["unbounded_statuses"] = dict(report.statuses)
    MEASURED["unbounded_p50_ms"] = _p(report.latencies_ms, 0.50)
    MEASURED["unbounded_p99_ms"] = _p(report.latencies_ms, 0.99)
    bounded_p99 = float(MEASURED["bounded_p99_ms"])  # type: ignore[arg-type]
    unbounded_p99 = _p(report.latencies_ms, 0.99)
    assert unbounded_p99 > bounded_p99, (
        "an unbounded queue answered as fast as a bounded one — the load is not above capacity, "
        f"so this drill proves nothing (bounded p99 {bounded_p99} ms, unbounded {unbounded_p99} ms)"
    )
    MEASURED["p99_ratio_unbounded_over_bounded"] = round(unbounded_p99 / max(bounded_p99, 1.0), 1)


def test_3_a_spent_budget_is_refused_without_running_the_model(stack):
    answer = httpx.post(
        stack["bounded"] + "/v2/models/jpcp/infer",
        json=INFER,
        headers={"X-ExaMLOps-Budget-Ms": "0"},
        timeout=10,
    )
    MEASURED["spent_budget_status"] = answer.status_code
    assert answer.status_code == 504
    assert "deadline" in answer.json().get("error", "").lower()


def test_4_the_server_is_healthy_after_the_burst(stack):
    assert httpx.get(stack["bounded"] + "/ready", timeout=10).status_code == 200
    assert httpx.get(stack["bounded"] + "/models", timeout=10).json() != []
    answer = httpx.post(stack["bounded"] + "/v2/models/jpcp/infer", json=INFER, timeout=30)
    assert answer.status_code == 200, answer.text
    assert answer.json()["outputs"][0]["data"], answer.text
    MEASURED["after_burst_prediction"] = answer.json()["outputs"][0]["data"]
    latencies = [_timed(stack["bounded"] + "/v2/models/jpcp/infer") for _ in range(20)]
    MEASURED["quiet_p50_ms"] = round(statistics.median(latencies), 1)


def _timed(url: str) -> float:
    began = time.perf_counter()
    httpx.post(url, json=INFER, timeout=30).raise_for_status()
    return (time.perf_counter() - began) * 1000
