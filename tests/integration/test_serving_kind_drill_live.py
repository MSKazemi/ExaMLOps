"""Chaos drill: the serving plane loses pods on Kubernetes (plan P5).

The control plane has a failover drill on a cluster
(`tests/integration/test_control_plane_failover_kind_live.py`); the serving plane had none, so
`docs/guides/game-days.md` listed Kubernetes-level serving failures as a gap. This closes it.

In a throwaway kind cluster: a real MLflow, a real sklearn model registered in it, and the model
server from an image built from this tree — deployed **twice**, so the drill can measure what the
deployment settings are worth rather than assert that they matter:

- `ray-serving` carries the settings this repo recommends (a readiness probe on `/ready`, a
  `preStop` pause, a grace period longer than a request, and a rollout that adds a pod before it
  takes one away);
- `ray-serving-naive` is the same image with Kubernetes' defaults and no `preStop`.

Both are put under load through four kinds of churn, and every claim below is a measurement the
drill prints rather than an expectation it asserts into existence:

- **a graceful pod deletion** (what an eviction, a node drain or a scale-down does) loses only the
  requests on the keep-alive connections pinned to that pod, all in one instant. A Service is
  layer 4: taking a pod out of its endpoints stops new connections, not established ones, and the
  server shuts Ray Serve down as soon as SIGTERM arrives;
- **a rolling upgrade** costs the same cut once per pod replaced, and no loss of capacity — that is
  what `maxUnavailable: 0` buys;
- **a pod killed outright** (a node lost, OOM, a hard crash) loses no more than that, and briefly;
- **one retry erases all of it**, which is what the platform's gateway already does
  (`retry_on: …,reset`, two attempts) and what a direct client is told to do;
- **a pod with no readiness probe is sent inference before its model is loaded** — the drill
  measures that on the naive deployment, which is what the probe is actually worth.

Opt-in and slow (a cluster, an image build, four model loads)::

    docker build -f serving/ray_serving/Dockerfile -t exa-chaos/ray-serving:tree .
    EXAMLOPS_KIND_SERVING_LIVE=1 .venv/bin/pytest \\
        tests/integration/test_serving_kind_drill_live.py -v -s
"""

from __future__ import annotations

import json
import os
import shutil
import time

import pytest

from tests.integration import _kind_serving as k
from tests.integration._kind_serving import (
    CONCURRENCY,
    DURATION,
    IMAGE,
    NODE_IMAGE,
    REPLICAS,
    Cluster,
)
from tests.integration.test_serving_overload_drill_live import INFER

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("EXAMLOPS_KIND_SERVING_LIVE") != "1", reason="set EXAMLOPS_KIND_SERVING_LIVE=1"
    ),
]

NS = "serving"
MEASURED: dict[str, object] = {}


@pytest.fixture(scope="module")
def cluster():
    for tool in ("kind", "kubectl", "docker"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is not installed")
    if k.run("docker", "image", "inspect", IMAGE, check=False).returncode:
        pytest.skip(f"build {IMAGE} first (see the module docstring)")
    c = Cluster(namespace=NS)
    try:
        k.run(
            "kind", "create", "cluster", "--name", c.name, "--image", NODE_IMAGE,
            "--kubeconfig", str(c.kubeconfig), "--wait", "120s", timeout=500,
        )  # fmt: skip
        k.run("kind", "load", "docker-image", IMAGE, "--name", c.name, timeout=900)
        k.run("kubectl", "create", "namespace", NS, env=c.env)
        k.mlflow_pod(c)
        k.register_model(c)
        for name, recommended in (("ray-serving", True), ("ray-serving-naive", False)):
            c.apply(k.serving_deployment(name, recommended=recommended))
            c.apply(k.service(name))
        for name in ("ray-serving", "ray-serving-naive"):
            c.kubectl("rollout", "status", f"deployment/{name}", "--timeout=600s", timeout=700)
        # The naive deployment has no readiness probe, so "rolled out" says nothing about whether
        # it can answer: wait for a real prediction from each Service before measuring anything.
        for name in ("ray-serving", "ray-serving-naive"):
            assert k.until(lambda n=name: k.predicts(c, n), timeout=420), f"{name} never served"
        yield c
    finally:
        if MEASURED:
            print("\nserving kind drill:", json.dumps(MEASURED, indent=1))
        k.run("kind", "delete", "cluster", "--name", c.name, check=False, timeout=300)


# ── the drill ─────────────────────────────────────────────────────────────────


def _delete(c: Cluster, pod: str) -> None:
    c.kubectl("delete", "pod", pod, "--wait=false")


def _kill_hard(c: Cluster, pod: str) -> None:
    """End a pod the way a lost node or an OOM kill does: no `preStop`, no grace period, no
    shutdown code. (`kill -9 1` from inside cannot do this — the kernel shields PID 1 of a PID
    namespace from signals sent within it.)"""
    c.kubectl("delete", "pod", pod, "--grace-period=0", "--force", "--wait=false")


def test_1_a_graceful_deletion_cuts_only_the_connections_pinned_to_that_pod(cluster):
    """What an eviction, a node drain and a scale-down all do, to a client holding connections.

    A Service is layer 4: taking a pod out of its endpoints stops *new* connections landing there,
    but a keep-alive connection already established stays pinned to that pod until something closes
    it — and the server shuts Ray Serve down as soon as SIGTERM arrives, so that something is the
    shutdown. Whatever those connections carried at that instant is lost. The number is the point:
    bounded by how many connections were pinned there, all in one instant, and erased by one retry
    (test 5) or by not holding connections at all (test 2).
    """
    result = k.during_load(cluster, "ray-serving", lambda pods: _delete(cluster, pods[0]))
    MEASURED["keepalive_graceful_delete"] = result
    MEASURED["keepalive_graceful_delete_lost"] = k.lost(result)
    assert result["sent"] > CONCURRENCY * DURATION, result  # the load really ran
    assert k.lost(result) <= CONCURRENCY, (
        f"a graceful deletion cost {k.lost(result)} of {result['sent']} requests, more than the "
        f"{CONCURRENCY} connections that could have been pinned to it: {result['failures'][:5]}"
    )
    instants = {round(float(offset)) for offset, _ in result["failures"]}
    assert len(instants) <= 2, f"the loss was spread over {sorted(instants)}"
    assert k.until(lambda: cluster.ready_endpoints("ray-serving") == REPLICAS, timeout=600), (
        "the replacement pod never joined the Service"
    )


def test_2_a_caller_that_does_not_hold_the_connection_loses_nothing(cluster):
    """The same deletion, for a caller that opens a connection per request — which is what the
    `preStop` pause and the readiness probe are there to protect. The pod leaves the endpoints
    first, so nothing new is ever routed to it, and it finishes what it had."""
    result = k.during_load(
        cluster, "ray-serving", lambda pods: _delete(cluster, pods[0]), keep_alive=False
    )
    MEASURED["fresh_graceful_delete"] = result
    assert k.lost(result) == 0, (
        f"a graceful deletion cost a connection-per-request caller {k.lost(result)} request(s), so "
        f"the pod was still being routed work after it was told to stop: {result['failures'][:5]}"
    )
    assert k.until(lambda: cluster.ready_endpoints("ray-serving") == REPLICAS, timeout=600)


def test_3_a_pod_with_no_readiness_probe_is_sent_work_before_it_can_serve(cluster):
    """What the readiness probe is worth, measured on the deployment that lacks it.

    The model takes tens of seconds to load. Without a readiness probe Kubernetes calls the pod
    ready as soon as its container starts, so the Service routes inference it cannot answer. This
    bites on every scale-up and every upgrade. Measured with a connection-per-request caller,
    because a caller holding connections never reaches a new endpoint at all — which is how the
    first version of this drill measured "nothing was lost" and proved nothing.
    """
    try:
        result = k.during_load(
            cluster,
            "ray-serving-naive",
            lambda _pods: cluster.kubectl(
                "scale", "deployment/ray-serving-naive", f"--replicas={REPLICAS + 1}"
            ),
            keep_alive=False,
        )
        MEASURED["scale_up_naive"] = result
        MEASURED["scale_up_naive_lost"] = k.lost(result)
        assert k.lost(result) > 0, (
            "a pod with no readiness probe answered everything anyway, so this drill cannot show "
            "what the probe is for — did the model load before the load ended?"
        )
    finally:
        cluster.kubectl("scale", "deployment/ray-serving-naive", f"--replicas={REPLICAS}")
        assert k.until(lambda: k.predicts(cluster, "ray-serving-naive"), timeout=600)


def test_4_the_same_scale_up_with_the_probe_costs_nothing(cluster):
    """The other half of test 3, and the measurement of how long a new replica takes to be worth
    routing to: the probe holds it out of the Service until its model is loaded."""
    started = 0.0

    def scale_up(_pods: list[str]) -> None:
        nonlocal started
        started = time.monotonic()
        cluster.kubectl("scale", "deployment/ray-serving", f"--replicas={REPLICAS + 1}")

    try:
        result = k.during_load(cluster, "ray-serving", scale_up, keep_alive=False)
        MEASURED["scale_up_with_probe"] = result
        assert k.lost(result) == 0, (
            f"a pod that had not loaded the model yet was sent traffic: {result['failures'][:5]}"
        )
        naive_lost = MEASURED.get("scale_up_naive_lost")
        assert naive_lost is None or int(naive_lost) > 0, (  # type: ignore[arg-type]
            "the same scale-up cost the probe-less deployment nothing, so the probe is unproven"
        )
        assert k.until(
            lambda: cluster.ready_endpoints("ray-serving") == REPLICAS + 1, timeout=600
        ), "the new pod never became ready"
        MEASURED["scale_up_seconds_to_ready"] = round(time.monotonic() - started, 1)
    finally:
        cluster.kubectl("scale", "deployment/ray-serving", f"--replicas={REPLICAS}")


def test_5_a_rolling_upgrade_costs_no_more_than_the_pods_it_replaces(cluster):
    """`maxUnavailable: 0` keeps capacity through an upgrade, so the only loss is the same
    keep-alive cut as test 1 — once per pod replaced, and nothing else."""
    result = k.during_load(
        cluster,
        "ray-serving",
        lambda _pods: cluster.kubectl("rollout", "restart", "deployment/ray-serving"),
    )
    MEASURED["rolling_upgrade"] = result
    MEASURED["rolling_upgrade_lost"] = k.lost(result)
    assert k.lost(result) <= CONCURRENCY * REPLICAS, (
        f"a rolling upgrade cost {k.lost(result)} of {result['sent']} requests: "
        f"{result['failures'][:5]}"
    )
    # Capacity never dipped: a surge rollout replaces a pod only once its replacement is ready, so
    # latency stays in the same range as a quiet run rather than piling onto one replica.
    assert result["p99_ms"] < 1000, result["p99_ms"]
    cluster.kubectl("rollout", "status", "deployment/ray-serving", "--timeout=600s", timeout=700)


def test_6_a_pod_killed_outright_only_loses_what_was_in_flight(cluster):
    """A lost node, an OOM kill, a hard crash: no shutdown code runs, so requests already on that
    pod cannot be saved. What must hold is that the loss is bounded and brief."""
    result = k.during_load(cluster, "ray-serving", lambda pods: _kill_hard(cluster, pods[0]))
    MEASURED["hard_kill"] = result
    failures = result["failures"]
    MEASURED["hard_kill_lost"] = len(failures)
    late = [f for f in failures if float(f[0]) > result["acted_at_about"] + 15]
    assert not late, f"requests were still failing 15 s after the kill: {late[:5]}"
    assert len(failures) <= CONCURRENCY * 10, (
        f"{len(failures)} requests lost to one pod: {failures[:5]}"
    )
    assert k.until(lambda: k.predicts(cluster, "ray-serving"), timeout=600)


def test_7_a_client_that_retries_once_sees_none_of_it(cluster):
    """The retry the platform's own front door already makes: the gateway routes inference with
    `retry_on: connect-failure,refused-stream,reset,retriable-status-codes` and two attempts
    (`platform/infra/docker-compose/gateway/envoy.yaml`). With another replica ready, every loss
    above becomes invisible — which is why a direct caller is told to do the same."""
    assert k.until(lambda: cluster.ready_endpoints("ray-serving") == REPLICAS, timeout=600), (
        "the drill needs both replicas ready before it can prove a retry hides a loss"
    )
    result = k.during_load(
        cluster, "ray-serving", lambda pods: _kill_hard(cluster, pods[0]), retries=1
    )
    MEASURED["hard_kill_with_one_retry"] = result
    assert result["failures"] == [], f"a single retry was not enough: {result['failures'][:5]}"


def test_8_the_serving_plane_is_whole_afterwards(cluster):
    assert k.until(lambda: cluster.ready_endpoints("ray-serving") == REPLICAS, timeout=600)
    answer = k.curl(cluster, "ray-serving", "/v2/models/jpcp/infer", body=INFER)
    assert answer.startswith("200"), answer
    MEASURED["after_everything"] = answer.strip()[:120]
    models = k.curl(cluster, "ray-serving", "/models")
    assert "jpcp" in models, models
