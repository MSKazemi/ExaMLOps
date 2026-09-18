"""Shared harness for the Kubernetes serving drills (plan P5).

Two drills put the model server under load in a throwaway kind cluster and break something:
`test_serving_kind_drill_live.py` takes pods away, `test_serving_node_loss_kind_live.py` takes a
whole node away. Everything they have in common lives here — the cluster, the MLflow they register
a model in, the Deployment under test, and the load client that measures what churn costs.

Not a test module: no `test_` names, so pytest imports it and collects nothing.

The load client is a **closed loop** — `concurrency` callers, each sending the next request as soon
as its answer arrives. That is deliberate, and it is the second thing these drills got wrong: an
open loop of 20 requests a second against a 6 ms service time leaves nothing in flight most of the
time, so killing a pod usually cost nothing and the drill "passed" having measured the gaps between
requests. `keep_alive` matters just as much, in the opposite direction: a caller that holds
connections never reaches a pod that joined *after* it started, which is how a deployment with no
readiness probe first appeared to lose nothing at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from tests.integration.test_serving_overload_drill_live import INFER, REGISTER

NODE_IMAGE = "kindest/node:v1.32.2"
IMAGE = "exa-chaos/ray-serving:tree"
REPLICAS = 2
CONCURRENCY = 8
DURATION = 45.0
DISRUPT_AT = 15.0  # seconds into the load

LOAD = r"""
import json, os, threading, time, httpx

URL = os.environ["TARGET"]
DURATION, RETRIES = float(os.environ["DURATION"]), int(os.environ["RETRIES"])
CONCURRENCY = int(os.environ["CONCURRENCY"])
KEEPALIVE = os.environ.get("KEEPALIVE", "1") == "1"
BODY = json.loads(os.environ["BODY"])
HEADERS = json.loads(os.environ.get("HEADERS") or "{}")
lock = threading.Lock()
codes, failures, latencies = {}, [], []
transport = 0
sent = 0
start = time.monotonic()

def caller():
    global sent, transport
    # Without keep-alive every request opens its own connection, so it is routed to whichever
    # endpoint the Service holds *now* — which is the only way a caller ever reaches a pod that
    # joined after it started.
    limits = httpx.Limits() if KEEPALIVE else httpx.Limits(max_keepalive_connections=0)
    client = httpx.Client(timeout=10.0, limits=limits)
    while time.monotonic() - start < DURATION:
        offset = round(time.monotonic() - start, 2)
        began = time.perf_counter()
        outcome = None
        for attempt in range(RETRIES + 1):
            try:
                r = client.post(URL, json=BODY, headers=HEADERS)
                with lock:
                    codes[str(r.status_code)] = codes.get(str(r.status_code), 0) + 1
                if r.status_code >= 500 and attempt < RETRIES:
                    continue
                outcome = None if r.status_code == 200 else str(r.status_code)
                break
            except httpx.HTTPError as exc:
                if attempt < RETRIES:
                    continue
                with lock:
                    transport += 1
                outcome = type(exc).__name__
                break
        with lock:
            sent += 1
            latencies.append((time.perf_counter() - began) * 1000)
            if outcome is not None:
                failures.append([offset, outcome])
    client.close()

threads = [threading.Thread(target=caller) for _ in range(CONCURRENCY)]
for t in threads:
    t.start()
for t in threads:
    t.join()
ordered = sorted(latencies)
print("RESULT " + json.dumps({
    "sent": sent, "codes": codes, "transport_errors": transport, "failures": failures,
    "concurrency": CONCURRENCY, "keep_alive": KEEPALIVE,
    "p50_ms": round(ordered[len(ordered) // 2], 1) if ordered else -1,
    "p99_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))], 1) if ordered else -1,
}))
"""


def run(*args: str, check: bool = True, timeout: float = 600, **kw) -> subprocess.CompletedProcess:
    out = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, **kw)
    if check and out.returncode != 0:
        raise AssertionError(f"{' '.join(args[:4])}…: {out.stderr[-3000:]}")
    return out


class Cluster:
    def __init__(self, namespace: str = "serving", prefix: str = "exa-serving") -> None:
        self.namespace = namespace
        self.name = f"{prefix}-{uuid.uuid4().hex[:6]}"
        self.dir = Path(tempfile.mkdtemp(prefix=f"exa-kind-{prefix}-"))
        self.kubeconfig = self.dir / "kubeconfig"
        self.env = {**os.environ, "KUBECONFIG": str(self.kubeconfig)}

    def kubectl(self, *args: str, check: bool = True, timeout: float = 300):
        return run(
            "kubectl", "-n", self.namespace, *args, check=check, timeout=timeout, env=self.env
        )

    def apply(self, manifest: dict) -> None:
        path = self.dir / f"{uuid.uuid4().hex[:6]}.json"
        path.write_text(json.dumps(manifest))
        self.kubectl("apply", "-f", str(path))

    def pods(self, app: str, running_only: bool = True) -> list[str]:
        args = ["get", "pods", "-l", f"app={app}"]
        if running_only:
            args.append("--field-selector=status.phase=Running")
        args += ["-o", "jsonpath={.items[*].metadata.name}"]
        return self.kubectl(*args).stdout.split()

    def ready_pods(self, app: str) -> list[str]:
        """Pods this app is actually serving from: Running, container Ready, not being deleted.

        `pods()` is not enough to pick a victim for a node-loss drill. It lists Running pods, and a
        pod left over from a rollout is Running while it terminates — stopping *its* node takes away
        something the Service was not using, and the drill then measures a node loss that cost
        nothing because nothing was there.

        Read as JSON rather than through a jsonpath: the first version separated fields with
        `{'\\t'}`, which in a plain Python string is a real tab, and kubectl answers that with
        "unterminated quoted string".
        """
        out = self.kubectl("get", "pods", "-l", f"app={app}", "-o", "json")
        items = json.loads(out.stdout or "{}").get("items", [])
        return [
            item["metadata"]["name"]
            for item in items
            if item.get("status", {}).get("phase") == "Running"
            and not item["metadata"].get("deletionTimestamp")
            and all(c.get("ready") for c in item.get("status", {}).get("containerStatuses", []))
            and item.get("status", {}).get("containerStatuses")
        ]

    def endpoint_state(self, service: str, pod: str) -> str | None:
        """Is `pod` still a *ready* endpoint of `service`? "ready", "notready", or None if gone.

        This, not the pod object, is what decides whether a dead pod still gets traffic. Kubernetes
        never deletes a pod whose node is unreachable — the kubelet that would confirm it is gone,
        so the pod stays `Terminating` until the node comes back (still there after ten minutes in
        the node-loss drill). What ends the black hole is the EndpointSlice dropping it.
        """
        out = self.kubectl(
            "get", "endpointslice", "-l", f"kubernetes.io/service-name={service}", "-o", "json"
        )
        for slice_ in json.loads(out.stdout or "{}").get("items", []):
            for endpoint in slice_.get("endpoints", []):
                if (endpoint.get("targetRef") or {}).get("name") == pod:
                    return "ready" if endpoint.get("conditions", {}).get("ready") else "notready"
        return None

    def ready_endpoints(self, service: str) -> int:
        """How many pod addresses the Service is actually sending traffic to."""
        out = self.kubectl(
            "get", "endpointslice", "-l", f"kubernetes.io/service-name={service}",
            "-o", "jsonpath={range .items[*]}{range .endpoints[*]}{.conditions.ready}{'\\n'}{end}{end}",
        )  # fmt: skip
        return sum(1 for line in out.stdout.split() if line.strip() == "true")


def serving_deployment(
    name: str,
    *,
    recommended: bool,
    replicas: int = REPLICAS,
    spread: str | None = None,
    placement: dict | None = None,
) -> dict:
    """The model server as a Deployment. `recommended` is what this repo's guide tells operators to
    set; the other is Kubernetes' own defaults, which is what you get by writing the obvious YAML."""
    container: dict = {
        "name": "ray-serving",
        "image": IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": 8001, "name": "http"}],
        "env": [
            {"name": "MLFLOW_TRACKING_URI", "value": "http://mlflow:5000"},
            {"name": "RAY_NUM_REPLICAS", "value": "1"},
            {"name": "RAY_PRELOAD_ALIASES", "value": "Production"},
            {"name": "RAY_SNAPSHOT_MODE", "value": "off"},
            {"name": "RAY_SERVE_GRPC_PORT", "value": "0"},
            {"name": "OTEL_SDK_DISABLED", "value": "true"},
        ],
        "resources": {
            "requests": {"cpu": "300m", "memory": "1500Mi"},
            "limits": {"cpu": "2", "memory": "4Gi"},
        },
        "volumeMounts": [{"name": "shm", "mountPath": "/dev/shm"}],
    }
    if recommended:
        # The readiness probe is the only way Kubernetes learns that the model is loaded: without
        # it the Service sends work to a pod that is still starting.
        container["readinessProbe"] = {
            "httpGet": {"path": "/ready", "port": 8001},
            "initialDelaySeconds": 5,
            "periodSeconds": 2,
            "failureThreshold": 90,  # the model load is tens of seconds
        }
        # A pause between the pod leaving the Service's endpoints and the process being asked to
        # stop. Endpoint removal is asynchronous, so without it a request can arrive after SIGTERM.
        container["lifecycle"] = {
            "preStop": {"exec": {"command": ["sleep", "8"]}},
        }
    pod_spec: dict = {
        "containers": [container],
        "volumes": [{"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "1Gi"}}],
        "terminationGracePeriodSeconds": 40 if recommended else 30,
    }
    if spread:
        # Spread the replicas over nodes, because two pods on one node are one node's worth of
        # availability. `whenUnsatisfiable` is the whole decision: `ScheduleAnyway` treats it as a
        # preference, so a lost node's replica is rescheduled onto the survivor; `DoNotSchedule`
        # makes it a rule, and then the replacement stays Pending until a node comes back.
        pod_spec["topologySpreadConstraints"] = [
            {
                "maxSkew": 1,
                "topologyKey": "kubernetes.io/hostname",
                "whenUnsatisfiable": spread,
                "labelSelector": {"matchLabels": {"app": name}},
            }
        ]
    if placement:
        pod_spec.update(placement)
    spec: dict = {
        "replicas": replicas,
        "selector": {"matchLabels": {"app": name}},
        "template": {"metadata": {"labels": {"app": name}}, "spec": pod_spec},
    }
    if recommended:
        # Add a pod before taking one away, so capacity never dips during an upgrade.
        spec["strategy"] = {
            "type": "RollingUpdate",
            "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
        }
        spec["minReadySeconds"] = 5
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name},
        "spec": spec,
    }


def service(name: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name},
        "spec": {
            "selector": {"app": name},
            "ports": [{"port": 8001, "targetPort": 8001, "name": "http"}],
        },
    }


def mlflow_pod(c: Cluster, placement: dict | None = None) -> None:
    c.apply(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "mlflow", "labels": {"app": "mlflow"}},
            "spec": {
                "containers": [
                    {
                        "name": "mlflow",
                        "image": IMAGE,
                        "imagePullPolicy": "IfNotPresent",
                        "command": [
                            "mlflow",
                            "server",
                            "--host",
                            "0.0.0.0",
                            "--port",
                            "5000",
                            "--backend-store-uri",
                            "sqlite:////tmp/mlflow.db",
                            "--serve-artifacts",
                            "--artifacts-destination",
                            "/tmp/artifacts",
                            "--allowed-hosts",
                            "mlflow,mlflow:5000,localhost,localhost:5000",
                        ],  # fmt: skip
                        "ports": [{"containerPort": 5000}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 5000},
                            "periodSeconds": 3,
                            "failureThreshold": 60,
                        },
                    }
                ],
                **(placement or {}),
            },
        }
    )
    c.kubectl("expose", "pod", "mlflow", "--port", "5000")
    c.kubectl("wait", "--for=condition=Ready", "pod/mlflow", "--timeout=300s")


def register_model(c: Cluster, placement: dict | None = None) -> None:
    """Train and register the model the servers will load, through the same MLflow they read."""
    script = c.dir / "register.py"
    script.write_text(REGISTER)
    c.kubectl("create", "configmap", "register", f"--from-file={script}")
    c.apply(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "register"},
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "register",
                        "image": IMAGE,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["python", "/w/register.py"],
                        "volumeMounts": [{"name": "w", "mountPath": "/w"}],
                    }
                ],
                "volumes": [{"name": "w", "configMap": {"name": "register"}}],
                **(placement or {}),
            },
        }
    )
    assert until(
        lambda: "registered" in c.kubectl("logs", "register", check=False).stdout, timeout=300
    ), c.kubectl("logs", "register", check=False).stdout


def until(fn, timeout: float, interval: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            found = fn()
        except Exception:  # noqa: BLE001 - the cluster is still settling
            found = None
        if found:
            return found
        time.sleep(interval)
    return None


PROBE = (
    "import json, os, httpx;"
    "body = os.environ.get('BODY');"
    "r = httpx.post(os.environ['URL'], json=json.loads(body), timeout=20) if body "
    "else httpx.get(os.environ['URL'], timeout=20);"
    "print(r.status_code, r.text[:200])"
)


def curl(c: Cluster, service: str, path: str, *, body: dict | None = None) -> str:
    """One request from inside the cluster, through the Service. The body travels in the
    environment, not inside the `-c` source: quoting it twice sent a JSON *string*, not an object.

    Tried up to three times, because `kubectl run --rm -i` sometimes returns nothing at all: it
    attaches to the pod after creating it, and when it loses that race the answer is lost with it.
    That made an otherwise green drill fail on its last assertion once in three runs.
    """
    for attempt in range(3):
        args = [
            "run", f"probe-{uuid.uuid4().hex[:6]}", "--rm", "-i", "--restart=Never",
            "--image", IMAGE, "--image-pull-policy", "IfNotPresent", "--quiet",
            f"--env=URL=http://{service}:8001{path}",
        ]  # fmt: skip
        if body is not None:
            args.append(f"--env=BODY={json.dumps(body)}")
        args += ["--command", "--", "python", "-c", PROBE]
        out = c.kubectl(*args, timeout=240, check=False).stdout.strip()
        if out[:3].isdigit():  # an HTTP status, so the pod's output really did come back
            return out
        if attempt == 2:
            return out
        time.sleep(3)
    return ""


def predicts(c: Cluster, service: str) -> bool:
    return curl(c, service, "/v2/models/jpcp/infer", body=INFER).startswith("200")


def load(
    c: Cluster,
    service: str,
    *,
    retries: int = 0,
    keep_alive: bool = True,
    duration: float = DURATION,
    placement: dict | None = None,
    headers: dict[str, str] | None = None,
    url: str | None = None,
    name: str | None = None,
) -> str:
    """Start the load pod (it runs for DURATION and prints RESULT). Returns its pod name."""
    pod = name or f"load-{uuid.uuid4().hex[:6]}"
    script = c.dir / f"{pod}.py"
    script.write_text(LOAD)
    c.kubectl("create", "configmap", pod, f"--from-file=load.py={script}")
    c.apply(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod},
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "load",
                        "image": IMAGE,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["python", "/w/load.py"],
                        "env": [
                            {
                                "name": "TARGET",
                                "value": url or f"http://{service}:8001/v2/models/jpcp/infer",
                            },
                            {"name": "HEADERS", "value": json.dumps(headers or {})},
                            {"name": "CONCURRENCY", "value": str(CONCURRENCY)},
                            {"name": "DURATION", "value": str(duration)},
                            {"name": "RETRIES", "value": str(retries)},
                            {"name": "KEEPALIVE", "value": "1" if keep_alive else "0"},
                            {"name": "BODY", "value": json.dumps(INFER)},
                        ],  # fmt: skip
                        "volumeMounts": [{"name": "w", "mountPath": "/w"}],
                    }
                ],
                "volumes": [{"name": "w", "configMap": {"name": pod}}],
                **(placement or {}),
            },
        }
    )
    return pod


def result_of(c: Cluster, pod: str, duration: float = DURATION) -> dict:
    """Wait for the load pod and parse what it measured."""
    c.kubectl("wait", f"pod/{pod}", "--for=jsonpath={.status.phase}=Succeeded",
              f"--timeout={int(duration) + 240}s", timeout=duration + 260)  # fmt: skip
    logs = c.kubectl("logs", pod).stdout
    line = [ln for ln in logs.splitlines() if ln.startswith("RESULT ")]
    assert line, logs[-2000:]
    return json.loads(line[-1][len("RESULT ") :])


def during_load(
    c: Cluster,
    service: str,
    action,
    *,
    retries: int = 0,
    keep_alive: bool = True,
    duration: float = DURATION,
    disrupt_at: float = DISRUPT_AT,
    placement: dict | None = None,
    headers: dict[str, str] | None = None,
    url: str | None = None,
) -> dict:
    """Run the load, do `action(pod_names)` `disrupt_at` seconds in, and return the measurement."""
    pod = load(
        c,
        service,
        retries=retries,
        keep_alive=keep_alive,
        duration=duration,
        placement=placement,
        headers=headers,
        url=url,
    )
    began = time.monotonic()
    assert until(lambda: c.kubectl("logs", pod, check=False).returncode == 0, timeout=180), (
        c.kubectl("describe", "pod", pod).stdout[-1500:]
    )
    sleep_for = disrupt_at - (time.monotonic() - began)
    if sleep_for > 0:
        time.sleep(sleep_for)
    acted = round(time.monotonic() - began, 1)
    action(c.pods(service))
    result = result_of(c, pod, duration=duration)
    result["acted_at_about"] = acted
    return result


def ok(result: dict) -> int:
    """Answered 200. JSON object keys are strings, so `codes` is keyed "200", not 200 — reading it
    with an int silently counted zero successes and made every comparison here vacuous."""
    return int(result["codes"].get("200", 0))


def lost(result: dict) -> int:
    return int(result["sent"]) - ok(result)
