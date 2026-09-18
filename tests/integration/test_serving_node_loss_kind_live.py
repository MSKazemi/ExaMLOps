"""Chaos drill: the serving plane loses a whole Kubernetes node (plan P5).

`tests/integration/test_serving_kind_drill_live.py` takes pods away one at a time, which is what an
eviction, an upgrade or a crash does. A node going away is a different failure, and the one people
plan for badly: nothing tells the cluster that the node is gone — it is *inferred*, slowly, from
missing heartbeats — so for as long as that takes, a Service keeps routing inference to a pod that
no longer exists.

A three-node kind cluster (one control plane, two workers), the model server spread one replica per
worker, and the callers and MLflow pinned to the control-plane node so they survive what happens to
the workers. Two ways to lose a node, which behave nothing alike:

- **a planned drain** (`kubectl drain`, the node-maintenance path): Kubernetes evicts the pods
  first, so it is the graceful deletion of the pod-churn drill, and it costs the same;
- **a node lost outright** (the machine's power, its kernel, its network): no eviction, no SIGTERM,
  and — the finding — the pod stays `Ready` in the API, because the kubelet that would say otherwise
  went with the node. The Service keeps routing its share of inference into a black hole, and each
  of those requests *hangs* for the caller's whole timeout. Measured: still failing 130 s after the
  node stopped, when the load ended; p99 10 s against a 16 ms median; it closes only when Kubernetes
  evicts the pod, `tolerationSeconds: 300` after the node is marked unreachable.

It also measures the cost of the scheduling rule underneath: with `whenUnsatisfiable:
DoNotSchedule`, a two-node cluster that loses a node cannot place the replacement anywhere and
serves at half capacity until the node returns. With `ScheduleAnyway` the replacement lands on the
survivor. Both are deployed, so the drill measures the difference rather than asserting a
preference.

Opt-in and slow (three nodes, a 1.9 GB image loaded into each, four model loads)::

    docker build -f serving/ray_serving/Dockerfile -t exa-chaos/ray-serving:tree .
    EXAMLOPS_KIND_NODE_LOSS_LIVE=1 .venv/bin/pytest \\
        tests/integration/test_serving_node_loss_kind_live.py -v -s
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from tests.integration import _kind_serving as k
from tests.integration._kind_serving import IMAGE, NODE_IMAGE, Cluster
from tests.integration.test_serving_overload_drill_live import INFER

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("EXAMLOPS_KIND_NODE_LOSS_LIVE") != "1",
        reason="set EXAMLOPS_KIND_NODE_LOSS_LIVE=1",
    ),
]

NS = "serving"
MEASURED: dict[str, object] = {}
# A node loss is not noticed for tens of seconds by design (the control plane waits out the missed
# heartbeats), so the load has to outlast that or it measures nothing.
NODE_LOSS_DURATION = 150.0
NODE_LOSS_DISRUPT_AT = 20.0
# What test 7 sets `tolerationSeconds` to, against Kubernetes' default of 300.
TOLERATION_SECONDS = 20

KIND_CONFIG = """kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
"""

# Everything that measures must outlive the worker that is killed, so it runs on the control-plane
# node — which needs the toleration for the taint that normally keeps workloads off it.
ON_CONTROL_PLANE = {
    "nodeSelector": {"node-role.kubernetes.io/control-plane": ""},
    "tolerations": [
        {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
    ],
}  # fmt: skip


def _workers(c: Cluster) -> list[str]:
    out = c.kubectl(
        "get", "nodes", "-l", "!node-role.kubernetes.io/control-plane",
        "-o", "jsonpath={.items[*].metadata.name}",
    )  # fmt: skip
    return out.stdout.split()


def _node_of(c: Cluster, pod: str) -> str:
    return c.kubectl("get", "pod", pod, "-o", "jsonpath={.spec.nodeName}").stdout.strip()


def _respread(c: Cluster, app: str) -> None:
    """Put one replica on each worker again, deterministically, and verify it.

    Needed because the drill's own earlier steps move pods around: draining a worker sends its
    replica to the survivor, and **nothing brings it back** — Kubernetes applies a spread constraint
    when a pod is scheduled and never again. Two replicas on one node is a perfectly healthy cluster
    by every check the drill makes, and a node loss measured from there is meaningless: stopping the
    empty node costs nothing, and stopping the full one is a total outage rather than the loss of one
    replica. (Both happened here before this existed, which is what the zero in an earlier run was.)

    A rollout restart is not enough: `whenUnsatisfiable: ScheduleAnyway` is a *score*, and it can
    lose to the other scores — three restarts in a row put both replicas back on the same worker. So
    the crowded node is cordoned while one of its pods is deleted, which leaves the scheduler exactly
    one place to put the replacement.
    """
    for node in _workers(c):
        c.kubectl("uncordon", node, check=False)
    for _ in range(3):
        placed = {p: _node_of(c, p) for p in c.ready_pods(app)}
        if len(set(placed.values())) >= 2:
            return
        crowded = next(iter(placed.values()))
        victim = next(p for p, node in placed.items() if node == crowded)
        c.kubectl("cordon", crowded)
        try:
            c.kubectl("delete", "pod", victim, "--wait=false")
            assert k.until(lambda: c.ready_endpoints(app) == k.REPLICAS, timeout=600, interval=5), (
                f"the replacement for {victim} never became ready"
            )
        finally:
            c.kubectl("uncordon", crowded, check=False)
    placed = {p: _node_of(c, p) for p in c.ready_pods(app)}
    raise AssertionError(
        f"{app} would not spread over both workers: {placed}; "
        f"nodes: {c.kubectl('get', 'nodes', '-o', 'wide').stdout}"
    )


def _victim_node(c: Cluster, app: str) -> str:
    """A node the Service is really serving `app` from, with a second one elsewhere.

    Both halves matter. Stopping a node whose pod is not in the Service measures nothing (a
    rollout's terminating pod is still `Running`), and stopping the *only* serving node measures an
    outage rather than the loss of one replica.
    """
    pods = c.ready_pods(app)
    nodes = {p: _node_of(c, p) for p in pods}
    assert len(set(nodes.values())) == 2, (
        f"{app} is not serving from two nodes, so a node loss cannot be measured: {nodes}"
    )
    assert c.ready_endpoints(app) == k.REPLICAS, (
        f"the Service has {c.ready_endpoints(app)} ready endpoints, not {k.REPLICAS}"
    )
    return nodes[pods[0]]


def _phase_counts(c: Cluster, app: str) -> dict[str, int]:
    out = c.kubectl("get", "pods", "-l", f"app={app}", "-o", "jsonpath={.items[*].status.phase}")
    counts: dict[str, int] = {}
    for phase in out.stdout.split():
        counts[phase] = counts.get(phase, 0) + 1
    return counts


@pytest.fixture(scope="module")
def cluster():
    for tool in ("kind", "kubectl", "docker"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is not installed")
    if k.run("docker", "image", "inspect", IMAGE, check=False).returncode:
        pytest.skip(f"build {IMAGE} first (see the module docstring)")
    c = Cluster(namespace=NS, prefix="exa-nodeloss")
    try:
        config = c.dir / "kind.yaml"
        config.write_text(KIND_CONFIG)
        k.run(
            "kind", "create", "cluster", "--name", c.name, "--image", NODE_IMAGE,
            "--config", str(config), "--kubeconfig", str(c.kubeconfig), "--wait", "180s",
            timeout=700,
        )  # fmt: skip
        k.run("kind", "load", "docker-image", IMAGE, "--name", c.name, timeout=1800)
        k.run("kubectl", "create", "namespace", NS, env=c.env)
        k.mlflow_pod(c, placement=ON_CONTROL_PLANE)
        k.register_model(c, placement=ON_CONTROL_PLANE)
        for name, policy in (
            ("ray-serving", "ScheduleAnyway"),
            ("ray-serving-strict", "DoNotSchedule"),
        ):
            c.apply(k.serving_deployment(name, recommended=True, spread=policy))
            c.apply(k.service(name))
        for name in ("ray-serving", "ray-serving-strict"):
            c.kubectl("rollout", "status", f"deployment/{name}", "--timeout=900s", timeout=1000)
        for name in ("ray-serving", "ray-serving-strict"):
            assert k.until(lambda n=name: k.predicts(c, n), timeout=420), f"{name} never served"
        # The drill is about losing a node, so both replicas must not be on the same one.
        for name in ("ray-serving", "ray-serving-strict"):
            nodes = {_node_of(c, p) for p in c.pods(name)}
            assert len(nodes) == 2, f"{name} did not spread over the workers: {nodes}"
        yield c
    finally:
        if MEASURED:
            print("\nnode loss drill:", json.dumps(MEASURED, indent=1))
        k.run("kind", "delete", "cluster", "--name", c.name, check=False, timeout=300)


# ── the drill ─────────────────────────────────────────────────────────────────


def test_1_draining_a_node_is_the_graceful_case(cluster):
    """`kubectl drain` is the maintenance path: the node is cordoned and its pods are *evicted*,
    which is a graceful deletion each. It must therefore cost what a graceful deletion costs — the
    keep-alive connections pinned to that pod, and nothing more."""
    worker = _workers(cluster)[0]
    MEASURED["drained_node"] = worker

    def drain(_pods):
        cluster.kubectl(
            "drain", worker, "--ignore-daemonsets", "--delete-emptydir-data", "--force",
            "--timeout=120s", timeout=200,
        )  # fmt: skip

    result = k.during_load(cluster, "ray-serving", drain, placement=ON_CONTROL_PLANE)
    MEASURED["drain"] = result
    MEASURED["drain_lost"] = k.lost(result)
    assert k.lost(result) <= k.CONCURRENCY, (
        f"draining a node cost {k.lost(result)} of {result['sent']} requests, more than the "
        f"connections that could have been pinned to the evicted pod: {result['failures'][:5]}"
    )


def test_2_a_strict_spread_cannot_replace_the_evicted_replica(cluster):
    """The cost of `whenUnsatisfiable: DoNotSchedule` on a cluster with as many nodes as replicas:
    there is nowhere left that satisfies the rule, so the replacement stays Pending and the
    deployment serves at half capacity until a node comes back. The preference-shaped constraint
    puts it on the surviving node instead."""
    assert k.until(lambda: cluster.ready_endpoints("ray-serving") == k.REPLICAS, timeout=420), (
        "the ScheduleAnyway deployment never got its replica back onto the surviving node"
    )
    MEASURED["scheduleanyway_endpoints_after_drain"] = cluster.ready_endpoints("ray-serving")

    strict = _phase_counts(cluster, "ray-serving-strict")
    MEASURED["strict_phases_after_drain"] = strict
    MEASURED["strict_endpoints_after_drain"] = cluster.ready_endpoints("ray-serving-strict")
    assert strict.get("Pending", 0) >= 1, (
        f"the strict deployment placed its replacement anyway ({strict}), so this comparison says "
        "nothing about the constraint"
    )
    assert cluster.ready_endpoints("ray-serving-strict") == k.REPLICAS - 1
    # Half the capacity, but still serving: an unplaceable replacement is not an outage.
    assert k.predicts(cluster, "ray-serving-strict")


def test_3_a_node_lost_outright_black_holes_its_share_of_the_traffic(cluster):
    """The machine's power, its kernel or its network. Nothing is evicted, nothing is signalled,
    and — this is the finding — **the pod stays `Ready` in the API**, because the kubelet that
    would say otherwise is gone with the node. The Service therefore keeps routing its share of
    inference into a black hole until Kubernetes gives up on the node and deletes the pod, which
    by default is `tolerationSeconds: 300` after it is marked unreachable.

    Measured twice, with different shapes and the same verdict: 43% of requests lost over two
    minutes in one run (failing fast, p99 154 ms) and 9% in another (hanging for the caller's whole
    10 s timeout). Which one you get depends on whether the dead node's packets are refused or
    dropped; neither is acceptable, and both last until Kubernetes gives up on the node.

    The assertions below deliberately pin the *bad* behaviour, because it is the platform's
    documented reality and the reason the guide asks for ejection at the proxy rather than trust in
    the cluster. If one of them ever fails, Kubernetes (or your configuration) has become better
    than this and [the guide](../../docs/guides/serving-on-kubernetes.md) needs the new number.
    """
    _respread(cluster, "ray-serving")
    victim_node = _victim_node(cluster, "ray-serving")
    MEASURED["lost_node"] = victim_node
    victim_pod = next(
        p for p in cluster.ready_pods("ray-serving") if _node_of(cluster, p) == victim_node
    )
    MEASURED["lost_pod"] = victim_pod
    container = victim_node  # kind names the node's container after the node

    def stop_node(_pods):
        MEASURED["node_stopped_at"] = time.monotonic()
        k.run("docker", "stop", container, timeout=180)

    result = k.during_load(
        cluster,
        "ray-serving",
        stop_node,
        keep_alive=False,  # a fresh connection each time: this measures the Service's own routing
        duration=NODE_LOSS_DURATION,
        disrupt_at=NODE_LOSS_DISRUPT_AT,
        placement=ON_CONTROL_PLANE,
    )
    MEASURED["node_loss"] = {kk: vv for kk, vv in result.items() if kk != "failures"}
    MEASURED["node_loss_lost"] = k.lost(result)
    offsets = [float(offset) for offset, _ in result["failures"]]
    MEASURED["node_loss_first_failure_offset"] = round(min(offsets), 1) if offsets else None
    MEASURED["node_loss_last_failure_offset"] = round(max(offsets), 1) if offsets else None
    if offsets:
        MEASURED["node_loss_error_window_seconds"] = round(max(offsets) - min(offsets), 1)
    assert offsets, (
        "losing a node cost nothing at all, which cannot be right with a caller opening a "
        "connection per request — is the load reaching both replicas?"
    )
    # It started the moment the node stopped, not later: nothing shields the caller.
    assert min(offsets) < NODE_LOSS_DISRUPT_AT + 10, MEASURED["node_loss_first_failure_offset"]
    # And it was still going a minute later, with nobody touching the cluster. This is the number
    # the guide quotes; if it ever drops, read the docstring above.
    assert max(offsets) > NODE_LOSS_DISRUPT_AT + 60, (
        "Kubernetes stopped routing to the lost node within a minute, which is better than this "
        f"platform documents ({MEASURED['node_loss_error_window_seconds']} s window measured): "
        "confirm it and update docs/guides/serving-on-kubernetes.md"
    )
    # How it fails is not fixed, and the drill must not pretend otherwise: whether the stopped
    # node's packets are refused or silently dropped decides whether the caller gets an error at
    # once or waits out its whole timeout. Both have been measured here — p99 154 ms in one run,
    # 10 s (the client timeout) in another — so the mode is recorded, not asserted.
    MEASURED["node_loss_p99_ms"] = result["p99_ms"]
    MEASURED["node_loss_share_lost"] = round(k.lost(result) / max(result["sent"], 1), 3)
    # Meanwhile the surviving replica keeps answering, so this is a partial black hole and not an
    # outage: whatever reaches the healthy pod is served normally throughout.
    assert k.ok(result) > result["sent"] * 0.5, (k.ok(result), result["sent"])
    mix: dict[str, int] = {}
    for _, kind in result["failures"]:
        mix[kind] = mix.get(kind, 0) + 1
    MEASURED["node_loss_failure_mix"] = dict(sorted(mix.items(), key=lambda kv: -kv[1]))
    answered = sum(count for kind, count in mix.items() if kind.isdigit())
    # Almost everything is a transport error, because the address simply stops answering. A few
    # HTTP answers are legitimate and documented: a request caught on a replica as it dies gets a
    # 500 from Ray's proxy (docs/components/ray-serve.md), and the platform is entitled to say so.
    # What would be wrong is the *platform* being the main source of errors during a node loss.
    assert answered <= len(result["failures"]) * 0.1, (
        f"most failures during a node loss were answers from the platform, not a dead address: "
        f"{MEASURED['node_loss_failure_mix']}"
    )


def test_4_the_black_hole_closes_when_the_lost_pod_leaves_the_service(cluster):
    """Nobody intervenes. The wait here *is* the outage the previous test measured the start of, and
    what ends it is the EndpointSlice dropping the pod on the unreachable node.

    Not the pod's deletion: Kubernetes never deletes a pod whose node is unreachable, because the
    kubelet that would confirm it went with the node. It stays `Terminating` until the node comes
    back — an earlier version of this test waited ten minutes for a deletion that cannot happen, and
    operators who see pods stuck `Terminating` after a node loss are seeing the same thing.
    """
    if "node_stopped_at" not in MEASURED:
        pytest.skip("no node was stopped (test 3 did not get that far)")
    # The one low-noise number in this drill: how long the pod on the dead node kept its place in
    # the Service. Request counts vary 20-fold between runs; this does not, and it is what
    # `tolerationSeconds` actually governs.
    victim_pod = str(MEASURED["lost_pod"])
    assert k.until(
        lambda: cluster.endpoint_state("ray-serving", victim_pod) != "ready",
        timeout=600,
        interval=2,
    ), f"{victim_pod} is still a ready endpoint long after its node went away"
    MEASURED["seconds_until_the_lost_pod_left_the_service"] = round(
        time.monotonic() - float(MEASURED["node_stopped_at"]), 1
    )
    MEASURED["lost_pod_phase_afterwards"] = cluster.kubectl(
        "get", "pod", victim_pod, "-o", "jsonpath={.status.phase}", check=False
    ).stdout.strip()
    assert k.until(lambda: k.predicts(cluster, "ray-serving"), timeout=600), (
        "the survivor never took over"
    )
    MEASURED["endpoints_after_the_black_hole_closed"] = cluster.ready_endpoints("ray-serving")
    answer = k.curl(cluster, "ray-serving", "/v2/models/jpcp/infer", body=INFER)
    assert answer.startswith("200"), answer


def test_5_with_the_lost_pod_out_of_the_service_the_survivor_serves_everything(cluster):
    """The node is still down, and its pod is still there in `Terminating` — but out of the
    Service, which is what counts. A caller now sees nothing at all —
    which is what recovery looks like, and why the window above is about *routing*, not capacity.

    Note what this does **not** claim: that one retry would have covered the window itself. A retry
    is only useful if the next attempt goes somewhere else, and during the black hole it lands on
    the dead endpoint again as often as the Service picks it — after the caller has already waited
    out its timeout once. That is why the guide asks for ejection at the proxy.
    """
    MEASURED["endpoints_before_the_recovery_load"] = cluster.ready_endpoints("ray-serving")
    result = k.during_load(
        cluster,
        "ray-serving",
        lambda _pods: None,
        retries=1,
        keep_alive=False,
        placement=ON_CONTROL_PLANE,
    )
    MEASURED["after_node_loss_with_retry"] = {
        kk: vv for kk, vv in result.items() if kk != "failures"
    }
    assert k.lost(result) == 0, f"still failing with the node down: {result['failures'][:5]}"
    assert result["p99_ms"] < 2000, result["p99_ms"]  # and no hangs left over


def test_6_the_node_coming_back_restores_capacity(cluster):
    if "lost_node" not in MEASURED:
        pytest.skip("no node was lost (test 3 did not get that far)")
    k.run("docker", "start", str(MEASURED["lost_node"]), timeout=180)
    started = time.monotonic()
    assert k.until(
        lambda: cluster.ready_endpoints("ray-serving") == k.REPLICAS, timeout=900, interval=5
    ), "the returning node never got a replica back"
    MEASURED["seconds_to_full_capacity_after_the_node_returned"] = round(
        time.monotonic() - started, 1
    )
    # And the strict deployment, which could not place its replacement at all, is whole again too.
    assert k.until(
        lambda: cluster.ready_endpoints("ray-serving-strict") == k.REPLICAS, timeout=900, interval=5
    ), f"the strict deployment is still short: {_phase_counts(cluster, 'ray-serving-strict')}"


def test_7_a_shorter_unreachable_toleration_does_not_shorten_the_black_hole(cluster):
    """A negative result, kept because it is the advice everyone reaches for first.

    Kubernetes adds `node.kubernetes.io/unreachable:NoExecute` to every pod with
    `tolerationSeconds: 300`, and the obvious conclusion — mine included — is that shortening it
    shortens how long a lost node's pod keeps its share of the traffic. It does not. Measured on
    four times over two clusters: the pod left the Service after 132-139 s with the default and
    133-136 s with 20 s — noise, and once 3.5 s slower with the short toleration. What the toleration governs is when the pod is *marked for deletion*, and so
    when a replacement is scheduled; what ends the traffic loss is the EndpointSlice dropping the
    pod once the node is unreachable, which happens on its own schedule either way.

    So this test asserts only what holds: both configurations stop routing to the dead pod within a
    couple of minutes, and neither leaves it there indefinitely. The numbers are recorded so the
    comparison stays visible rather than becoming folklore.
    """
    patch = json.dumps(
        {
            "spec": {
                "template": {
                    "spec": {
                        "tolerations": [
                            {
                                "key": "node.kubernetes.io/unreachable",
                                "operator": "Exists",
                                "effect": "NoExecute",
                                "tolerationSeconds": TOLERATION_SECONDS,
                            },
                            {
                                "key": "node.kubernetes.io/not-ready",
                                "operator": "Exists",
                                "effect": "NoExecute",
                                "tolerationSeconds": TOLERATION_SECONDS,
                            },
                        ]
                    }
                }
            }
        }
    )
    if "seconds_until_the_lost_pod_left_the_service" not in MEASURED:
        pytest.skip("there is no default time to compare against (tests 3-4 did not run)")
    cluster.kubectl("patch", "deployment", "ray-serving", "--type=merge", "-p", patch)
    cluster.kubectl("rollout", "status", "deployment/ray-serving", "--timeout=900s", timeout=1000)
    _respread(cluster, "ray-serving")
    victim = _victim_node(cluster, "ray-serving")
    MEASURED["second_lost_node"] = victim
    MEASURED["short_toleration_endpoints_before_the_stop"] = cluster.ready_endpoints("ray-serving")
    victim_pod = next(
        p for p in cluster.ready_pods("ray-serving") if _node_of(cluster, p) == victim
    )
    stopped: dict[str, float] = {}

    def stop(_pods):
        stopped["at"] = time.monotonic()
        k.run("docker", "stop", victim, timeout=180)

    result = k.during_load(
        cluster,
        "ray-serving",
        stop,
        keep_alive=False,
        duration=NODE_LOSS_DURATION,
        disrupt_at=NODE_LOSS_DISRUPT_AT,
        placement=ON_CONTROL_PLANE,
    )
    try:
        MEASURED["node_loss_with_short_toleration"] = {
            kk: vv for kk, vv in result.items() if kk != "failures"
        }
        offsets = [float(offset) for offset, _ in result["failures"]]
        window = round(max(offsets) - min(offsets), 1) if offsets else 0.0
        MEASURED["short_toleration_error_window_seconds"] = window
        MEASURED["short_toleration_lost"] = k.lost(result)
        # What the toleration actually governs, measured the same way as test 4: how long the pod
        # on the dead node keeps existing, and therefore keeps its place in the Service.
        assert k.until(
            lambda: cluster.endpoint_state("ray-serving", victim_pod) != "ready",
            timeout=600,
            interval=2,
        ), f"{victim_pod} is still a ready endpoint"
        evicted_in = round(time.monotonic() - stopped["at"], 1)
        MEASURED["short_toleration_seconds_until_the_pod_left_the_service"] = evicted_in
        MEASURED["short_toleration_share_lost"] = round(k.lost(result) / max(result["sent"], 1), 3)
        default_evicted_in = float(MEASURED["seconds_until_the_lost_pod_left_the_service"])  # type: ignore[arg-type]
        MEASURED["toleration_changed_the_black_hole_by_seconds"] = round(
            evicted_in - default_evicted_in, 1
        )
        # Both bounded, neither indefinite. No assertion that one beats the other: it does not.
        assert evicted_in < 300, f"the dead pod kept its place in the Service for {evicted_in} s"
        assert default_evicted_in < 300, default_evicted_in
        # Deliberately NOT compared: how many requests each cost. Across runs the same default
        # configuration cost 121, 146, 670 and 2151 requests — a 20-fold spread that swamps anything
        # this change does to it, so comparing counts here would be noise dressed up as a finding.
    finally:
        k.run("docker", "start", victim, check=False, timeout=180)
        k.until(
            lambda: cluster.ready_endpoints("ray-serving") == k.REPLICAS, timeout=900, interval=5
        )


# ── the same node loss, but behind the gateway ────────────────────────────────
#
# The two iterations before this one ended with a claim the documentation now rests on: a caller
# behind the serving gateway does not see a node loss, because the gateway probes `/ready` and stops
# choosing an endpoint that has stopped answering (measured in
# `tests/integration/test_serving_gateway_ejection_live.py`, ~8 s) long before Kubernetes stops
# routing to it (measured above, ~130 s). That was proven for Envoy against two stub endpoints. This
# proves it for the real thing: a real gateway, the committed configuration, real model servers, and
# a node actually stopped.
#
# It needs the gateway to resolve **one endpoint per pod**, which is what a headless Service gives
# it. Everything here is what the guide tells an operator to deploy.

CONTROL_PLANE_IMAGE = "exa-chaos/examlops-control-plane:tree"
ENVOY_IMAGE = "envoyproxy/envoy:v1.39.1"
GATEWAY_CONFIG = (
    Path(__file__).resolve().parents[2]
    / "platform"
    / "infra"
    / "docker-compose"
    / "gateway"
    / "envoy.yaml"
)


def _headless_service(name: str, selector: str) -> dict:
    """A Service with no cluster IP: DNS answers with one A record per ready pod, so a proxy in
    front of it load-balances over pods and can take one out. With an ordinary ClusterIP there is
    a single address and nothing to eject."""
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name},
        "spec": {
            "clusterIP": "None",
            "selector": {"app": selector},
            "ports": [
                {"port": 8001, "targetPort": 8001, "name": "http"},
                {"port": 8081, "targetPort": 8081, "name": "grpc"},
            ],
        },
    }


def _authz_manifests() -> list[dict]:
    """The gateway's authorization service, as Compose runs it: the control-plane image serving
    `examlops.serving_gateway`, with its own SQLite state."""
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "gateway-authz"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "gateway-authz"}},
                "template": {
                    "metadata": {"labels": {"app": "gateway-authz"}},
                    "spec": {
                        "containers": [
                            {
                                "name": "authz",
                                "image": CONTROL_PLANE_IMAGE,
                                "imagePullPolicy": "IfNotPresent",
                                "command": [
                                    "uvicorn",
                                    "--factory",
                                    "examlops.serving_gateway:create_app",
                                    "--host",
                                    "0.0.0.0",
                                    "--port",
                                    "8090",
                                ],  # fmt: skip
                                "env": [
                                    {"name": "PLATFORM_DB", "value": "/state/platform.db"},
                                    {"name": "EXAMLOPS_ACTOR", "value": "serving-gateway"},
                                    {"name": "OTEL_SDK_DISABLED", "value": "true"},
                                    # The per-tenant quota is 600 requests a minute by default, and
                                    # this drill's eight concurrent callers send about that in ten
                                    # seconds: the first run measured a working quota (600 × 200,
                                    # then 429) and nothing about availability. The quota has its
                                    # own test in tests/integration/test_serving_gateway_live.py.
                                    {"name": "EXAMLOPS_GATEWAY_TENANT_RPM", "value": "0"},
                                ],
                                "ports": [{"containerPort": 8090}],
                                "readinessProbe": {
                                    "httpGet": {"path": "/healthz", "port": 8090},
                                    "periodSeconds": 2,
                                    "failureThreshold": 60,
                                },
                                "volumeMounts": [{"name": "state", "mountPath": "/state"}],
                            }
                        ],
                        "volumes": [{"name": "state", "emptyDir": {}}],
                        **ON_CONTROL_PLANE,
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "gateway-authz"},
            "spec": {
                "selector": {"app": "gateway-authz"},
                "ports": [{"port": 8090, "targetPort": 8090}],
            },
        },
    ]


def _gateway_manifests() -> list[dict]:
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "gateway"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "gateway"}},
                "template": {
                    "metadata": {"labels": {"app": "gateway"}},
                    "spec": {
                        "containers": [
                            {
                                "name": "envoy",
                                "image": ENVOY_IMAGE,
                                "imagePullPolicy": "IfNotPresent",
                                # The chart passes the same flag: without a writable runtime
                                # directory Envoy's hot restart has nowhere to put its socket.
                                "args": [
                                    "-c",
                                    "/cfg/envoy.yaml",
                                    "--log-level",
                                    "warn",
                                    "--disable-hot-restart",
                                ],  # fmt: skip
                                "ports": [{"containerPort": 8080}],
                                "readinessProbe": {
                                    "httpGet": {"path": "/v2/health/live", "port": 8080},
                                    "periodSeconds": 2,
                                    "failureThreshold": 60,
                                },
                                "volumeMounts": [{"name": "cfg", "mountPath": "/cfg"}],
                            }
                        ],
                        "volumes": [{"name": "cfg", "configMap": {"name": "gateway-envoy"}}],
                        **ON_CONTROL_PLANE,
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "gateway"},
            "spec": {
                "selector": {"app": "gateway"},
                "ports": [{"port": 8080, "targetPort": 8080}],
            },
        },
    ]


@pytest.fixture(scope="module")
def gateway(cluster):
    """The serving gateway in front of the model server, reading the committed Envoy config.

    Only two words of that file are changed — the upstream host, from the ClusterIP Service this
    drill already has to the headless one — because that is the whole point: a proxy can only eject
    a pod it can see.
    """
    if k.run("docker", "image", "inspect", CONTROL_PLANE_IMAGE, check=False).returncode:
        pytest.skip(f"build {CONTROL_PLANE_IMAGE} first (the authorization service runs from it)")
    k.run("kind", "load", "docker-image", CONTROL_PLANE_IMAGE, "--name", cluster.name, timeout=1800)
    k.run("docker", "pull", ENVOY_IMAGE, timeout=600)
    k.run("kind", "load", "docker-image", ENVOY_IMAGE, "--name", cluster.name, timeout=900)

    cluster.apply(_headless_service("ray-serving-headless", "ray-serving"))
    for manifest in _authz_manifests():
        cluster.apply(manifest)
    config = GATEWAY_CONFIG.read_text().replace(
        "address: ray-serving, port_value:", "address: ray-serving-headless, port_value:"
    )
    path = cluster.dir / "gateway-envoy.yaml"
    path.write_text(config)
    cluster.kubectl("create", "configmap", "gateway-envoy", f"--from-file=envoy.yaml={path}")
    for manifest in _gateway_manifests():
        cluster.apply(manifest)
    for name in ("gateway-authz", "gateway"):
        cluster.kubectl("rollout", "status", f"deployment/{name}", "--timeout=300s", timeout=400)
    # A virtual key, issued in the authorization service's own store — the same call `exa` makes.
    authz_pod = cluster.ready_pods("gateway-authz")[0]
    issued = cluster.kubectl(
        "exec", authz_pod, "--", "python", "-c",
        "import os; os.environ['PLATFORM_DB']='/state/platform.db';"
        "from examlops import gateway;"
        "print(gateway.issue_virtual_key('acme', 'research', None, None, 'node-loss-drill'))",
    )  # fmt: skip
    key = issued.stdout.strip().splitlines()[-1]
    assert key, issued.stdout
    yield {"headers": {"Authorization": f"Bearer {key}"}}


def test_8_the_gateway_answers_through_the_headless_service(cluster, gateway):
    """Before measuring a failure, prove the path works: a credentialed caller reaches a model
    server through the gateway, and both pods are behind it."""
    _respread(cluster, "ray-serving")
    result = k.during_load(
        cluster,
        "ray-serving",
        lambda _pods: None,
        keep_alive=False,
        duration=20.0,
        disrupt_at=5.0,
        placement=ON_CONTROL_PLANE,
        headers=gateway["headers"],
        url="http://gateway:8080/v2/models/jpcp/infer",
    )
    MEASURED["through_the_gateway"] = {kk: vv for kk, vv in result.items() if kk != "failures"}
    assert k.lost(result) == 0, result["failures"][:5]
    # Both pods are behind it: a headless Service is what lets the proxy see them individually, and
    # the next test's whole argument depends on it.
    assert cluster.ready_endpoints("ray-serving-headless") == k.REPLICAS, (
        "the headless Service does not have both pods, so the gateway sees one endpoint"
    )


def test_9_behind_the_gateway_a_lost_node_costs_seconds_not_minutes(cluster, gateway):
    """The claim the last two iterations' documentation rests on, end to end.

    Directly against the Service, the same cluster lost requests for **130 s** — until Kubernetes
    dropped the dead pod from the EndpointSlice. Behind the gateway the pod is one endpoint of many,
    and the gateway's own `/ready` probe takes it out after two failures. The difference is the
    whole argument for putting the gateway in front of a headless Service.
    """
    _respread(cluster, "ray-serving")
    victim = _victim_node(cluster, "ray-serving")
    MEASURED["gateway_lost_node"] = victim
    stopped: dict[str, float] = {}

    def stop(_pods):
        stopped["at"] = time.monotonic()
        k.run("docker", "stop", victim, timeout=180)

    try:
        result = k.during_load(
            cluster,
            "ray-serving",
            stop,
            keep_alive=False,
            duration=NODE_LOSS_DURATION,
            disrupt_at=NODE_LOSS_DISRUPT_AT,
            placement=ON_CONTROL_PLANE,
            headers=gateway["headers"],
            url="http://gateway:8080/v2/models/jpcp/infer",
        )
        MEASURED["gateway_node_loss"] = {kk: vv for kk, vv in result.items() if kk != "failures"}
        MEASURED["gateway_node_loss_lost"] = k.lost(result)
        offsets = [float(offset) for offset, _ in result["failures"]]
        window = round(max(offsets) - min(offsets), 1) if offsets else 0.0
        MEASURED["gateway_error_window_seconds"] = window
        direct = float(MEASURED["node_loss_error_window_seconds"])  # type: ignore[arg-type]
        MEASURED["direct_error_window_seconds"] = direct
        # The measurement that matters: the failures stop while the node is still gone, because the
        # gateway stopped choosing it — not because the cluster noticed.
        assert window < direct / 2, (
            f"behind the gateway a lost node still cost {window} s of failures, against {direct} s "
            f"straight at the Service: the gateway is not ejecting the endpoint"
        )
        assert k.ok(result) > result["sent"] * 0.8, (k.ok(result), result["sent"])
    finally:
        k.run("docker", "start", victim, check=False, timeout=180)
        k.until(
            lambda: cluster.ready_endpoints("ray-serving") == k.REPLICAS, timeout=900, interval=5
        )
