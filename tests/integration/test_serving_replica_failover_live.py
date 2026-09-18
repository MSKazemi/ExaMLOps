"""Kill a serving replica mid-traffic: no request fails beyond the retry budget (plan P4.8).

A local Ray Serve runs a two-replica stand-in for the model server at the Open Inference Protocol
route. Requests go through the platform's real ``ModelRouter`` (deadline, retries on transport
errors and 503, the retry budget), and one replica is killed with ``ray.kill`` while they flow.
Ray routes around the dead replica and starts a replacement. The requests that were in flight on
it come back from Ray's proxy as a plain-text 500; the router retries those once. The assertions
are the plan's (zero failed requests) and that the kill really caught requests in flight: without
that the test would pass on Ray's own rerouting alone, as an earlier version of it did with the
router's retries switched off.

It starts its **own** private cluster (``address="local"``, a private temp dir, ``RAY_ADDRESS``
removed) and never uses cluster discovery: on a host that also runs the platform's Ray Serve, an
auto-discovering call could otherwise reach — and a kill could hit — the wrong cluster.

Opt-in, because it starts a Ray cluster in-process::

    EXAMLOPS_RAY_LIVE=1 .venv/bin/pytest tests/integration/test_serving_replica_failover_live.py -v
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "platform" / "cli" / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("EXAMLOPS_RAY_LIVE") != "1", reason="set EXAMLOPS_RAY_LIVE=1"),
]


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def serve_url(tmp_path_factory):
    ray = pytest.importorskip("ray")
    from fastapi import FastAPI
    from ray import serve

    os.environ.pop("RAY_ADDRESS", None)
    port, metrics_port = _port(), _port()
    ray.init(
        address="local",  # always a new private cluster, never an existing one
        num_cpus=4,
        include_dashboard=False,
        log_to_driver=False,
        _metrics_export_port=metrics_port,
        # Short: Ray's unix-socket paths live under it and have a length limit.
        _temp_dir=tempfile.mkdtemp(prefix="exa-ray-"),
    )
    serve.start(http_options={"host": "127.0.0.1", "port": port})
    app = FastAPI()

    @serve.deployment(num_replicas=2, ray_actor_options={"num_cpus": 0.5}, max_ongoing_requests=100)
    @serve.ingress(app)
    class StandIn:
        """Answers like the model server does: an OIP v2 response naming the replica."""

        @app.post("/v2/models/{name}/infer")
        async def infer(self, name: str) -> dict:
            await asyncio.sleep(0.2)  # long enough that a kill catches requests in flight
            return {
                "model_name": name,
                "model_version": "1",
                # The replica's own actor name, so the test can kill it without discovery.
                "parameters": {"run_id": ray.get_runtime_context().get_actor_name()},
                "outputs": [{"name": "predict", "datatype": "FP64", "shape": [1], "data": [1.0]}],
            }

    serve.run(StandIn.bind(), name="standin", route_prefix="/")
    yield {"url": f"http://127.0.0.1:{port}", "metrics": f"http://127.0.0.1:{metrics_port}"}
    serve.shutdown()
    ray.shutdown()


def test_killing_a_replica_fails_no_request(serve_url, monkeypatch):
    import ray

    from serving.budgets import Deadline, RetryBudget
    from serving.inference_pipeline import app as pipeline

    monkeypatch.setattr(pipeline, "_RAY_SERVE_URL", serve_url["url"])
    monkeypatch.setattr(pipeline, "_http_client", None)
    monkeypatch.setattr(pipeline, "_RETRY_BUDGET", RetryBudget())
    lost: list[int] = []
    seen: list[str] = []  # every non-503 answer the router classified, for the failure message

    def replica_lost(resp):
        answer = real_replica_lost(resp)
        seen.append(f"{resp.status_code}:{resp.headers.get('content-type', '')[:20]}")
        if answer:
            lost.append(resp.status_code)
        return answer

    real_replica_lost = pipeline._replica_lost
    monkeypatch.setattr(pipeline, "_replica_lost", replica_lost)

    # Learn the replicas' actor names from their own answers (no cluster discovery).
    async def warm() -> list[dict]:
        return await asyncio.gather(
            *(
                pipeline.ModelRouter._post("jpcp", "Production", {"x": 1.0}, Deadline.after(10))
                for _ in range(40)
            )
        )

    warmup = asyncio.run(warm())
    replicas = sorted({r["run_id"] for r in warmup})
    assert len(replicas) == 2, replicas
    monkeypatch.setattr(pipeline, "_http_client", None)  # the client is bound to its event loop

    async def traffic() -> list[dict]:
        results: list[dict] = []
        for i in range(40):
            batch = [
                asyncio.create_task(
                    pipeline.ModelRouter._post("jpcp", "Production", {"x": 1.0}, Deadline.after(10))
                )
                # Enough that both replicas are busy (Ray balances by power of two choices), so
                # the killed one always dies holding requests: with 8, all could land on the
                # other replica and the kill caught nothing.
                for _ in range(20)
            ]
            if i == 10:
                await asyncio.sleep(0.1)  # the batch is on the replicas now
                ray.kill(ray.get_actor(replicas[0], namespace="serve"), no_restart=False)
            results.extend(await asyncio.gather(*batch))
        return results

    started = time.monotonic()
    results = asyncio.run(traffic())
    failed = [r for r in results if "error" in r]
    assert not failed, f"{len(failed)} of {len(results)} failed: {failed[:3]}"
    non_200 = sorted({x for x in seen if not x.startswith("200")})
    assert lost, (
        "the kill caught no request in flight, so the router's retry was not exercised; "
        f"non-200 answers seen: {non_200}, retry budget {pipeline._RETRY_BUDGET.__dict__}"
    )
    served_by = {r["run_id"] for r in results}
    assert len(served_by) >= 2, served_by  # both replicas answered at some point
    assert time.monotonic() - started < 120

    # And Prometheus is told: the lost replica shows as retries, under the names the alerts use.
    import urllib.request

    body, deadline = "", time.monotonic() + 30
    while time.monotonic() < deadline:
        body = urllib.request.urlopen(serve_url["metrics"] + "/metrics", timeout=5).read().decode()
        if "ray_examlops_router_retries_total{" in body and 'reason="replica_lost"' in body:
            break
        time.sleep(2)
    assert 'reason="replica_lost"' in body, [
        line for line in body.splitlines() if "examlops_router" in line
    ]
    assert "ray_examlops_router_requests_total{" in body
