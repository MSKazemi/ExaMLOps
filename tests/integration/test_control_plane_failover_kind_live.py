"""Failover drill: three control-plane replicas on Kubernetes lose pods under load (plan P5).

The chart kept `controlPlane.replicaCount: 1` "until failover has been exercised on a cluster".
This drill exercises it. In a throwaway kind cluster it runs a Postgres, a Prefect stand-in that
behaves like Prefect's API for what the control plane uses (deployment lookup, idempotent
`create_flow_run`, flow-run state), and this chart with three control-plane replicas built from
this tree. An in-cluster client submits retrains through the Service without pause. Twice, the test
waits until a replica is in the middle of a dispatch (Prefect, here, takes 1.5 s to answer) and
crashes it: SIGKILL of its process from the node, so no shutdown code runs. Then:

- **nothing accepted is lost**: every retrain the control plane answered 202 reaches `succeeded`,
  including those a killed replica had accepted or claimed (another replica takes them over when
  the claim lease runs out);
- **nothing runs twice**: each accepted command has exactly one flow run, and no two commands share
  one, even when a killed replica's dispatch was retried by another;
- **a crash mid-dispatch is recovered**: the crashed replica's claimed command is dispatched again
  by a replica once the claim lease (`CONTROL_PLANE_COMMAND_LEASE_SECONDS`) runs out, with the same
  Prefect idempotency key, so Prefect returns the run it already created;
- **a retried submission is the same command**: the client resends a submission whose answer it
  lost with the same `Idempotency-Key`, possibly to another replica, and gets the same command;
- **the Service recovers**: the longest gap between two accepted submissions stays short.

Opt-in and slow (a cluster and an image build)::

    docker build -f platform/services/control_plane/Dockerfile \\
        -t exa-kind-spire/examlops-control-plane:test .
    EXAMLOPS_KIND_FAILOVER_LIVE=1 .venv/bin/pytest \\
        tests/integration/test_control_plane_failover_kind_live.py -v -s
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "platform" / "infra" / "helm" / "examlops"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("EXAMLOPS_KIND_FAILOVER_LIVE") != "1", reason="set EXAMLOPS_KIND_FAILOVER_LIVE=1"
    ),
]

NODE = "kindest/node:v1.32.2"
IMAGE = "exa-kind-spire/examlops-control-plane:test"
POSTGRES = "postgres:17-alpine"
NS = "mlops"
REPLICAS = 3
DURATION = 150  # seconds of load
KILLS_AT = (40, 90)  # seconds into the load
LEASE_SECONDS = 20  # CONTROL_PLANE_COMMAND_LEASE_SECONDS for the drill

# Prefect's API, as much of it as the control plane uses. create_flow_run is idempotent on its key
# (as Prefect is); every run is COMPLETED at once, so the next retrain of a model can start.
PREFECT_STUB = r"""
import json, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

lock = threading.Lock()
runs = {}      # flow run id -> idempotency key
by_key = {}    # idempotency key -> flow run id
calls = {"create_flow_run": 0}
inflight = {}  # idempotency key -> caller address, while a create_flow_run is being answered

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/api/health", "/api/ready"):
            return self._send(200, True)
        if path.startswith("/api/deployments/name/"):
            # The control plane refuses to dispatch what the deployment's schema does not accept.
            schema = {"properties": {p: {} for p in (
                "model_name", "dataset_cls_name", "is_dummy", "backend_name", "dataset_revision")}}
            return self._send(200, {"id": "deployment-1", "name": path.rsplit("/", 1)[-1],
                                    "parameter_openapi_schema": schema})
        if path.startswith("/api/flow_runs/"):
            run = path.rsplit("/", 1)[-1]
            if run not in runs:
                return self._send(404, {"detail": "not found"})
            return self._send(200, {"id": run, "state": {"type": "COMPLETED", "name": "Completed"}})
        if path == "/stub/runs":
            with lock:
                return self._send(200, {"calls": calls["create_flow_run"], "runs": runs})
        if path == "/stub/inflight":
            with lock:
                return self._send(200, sorted(set(inflight.values())))
        return self._send(404, {"detail": path})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.endswith("/create_flow_run"):
            # As slow as a real Prefect under load, so a replica can die holding a dispatch.
            key = body.get("idempotency_key") or str(uuid.uuid4())
            with lock:
                inflight[key] = self.client_address[0]
            time.sleep(1.5)
            with lock:
                inflight.pop(key, None)
                calls["create_flow_run"] += 1
                run = by_key.setdefault(key, str(uuid.uuid4()))
                runs[run] = key
            return self._send(201, {"id": run, "state": {"type": "SCHEDULED"}})
        return self._send(404, {"detail": self.path})

ThreadingHTTPServer(("0.0.0.0", 4200), Handler).serve_forever()
"""

# The load: retrains without pause, round-robin over the registry's model x dataset pairs. A lost
# answer (connection error, 5xx, 429) is retried with the SAME key; 409 means that pair still has a
# retrain in flight. At the end, every accepted command is followed to a terminal state.
CLIENT = r"""
import json, os, time, uuid, httpx
BASE = "http://rel-examlops-control-plane:8002"
H = {"Authorization": "Bearer " + os.environ["CONTROL_PLANE_TOKEN"]}
PAIRS = [("JPCP", "PM100Dataset"), ("JPCP", "FDataDataset"), ("MACK", "FDataDataset"),
         ("MCBound", "FDataDataset")]
DURATION, SETTLE = float(os.environ["DURATION"]), float(os.environ["SETTLE"])
accepted, retried_same, transient, gave_up, gaps = {}, 0, 0, 0, []
codes, samples = {}, {}
start = last_ok = time.monotonic()
client = httpx.Client(timeout=5.0)
i = 0
while time.monotonic() - start < DURATION:
    model, dataset = PAIRS[i % len(PAIRS)]
    i += 1
    key, first_id = str(uuid.uuid4()), None
    for attempt in range(30):
        try:
            r = client.post(BASE + "/v1/retrain", json={"model_name": model, "dataset_name": dataset},
                            headers={**H, "Idempotency-Key": key})
        except httpx.HTTPError:
            transient += 1
            time.sleep(0.5)
            continue
        codes[r.status_code] = codes.get(r.status_code, 0) + 1
        samples.setdefault(r.status_code, r.text[:300])
        if r.status_code in (429, 500, 502, 503, 504):
            transient += 1
            time.sleep(0.5)
            continue
        if r.status_code == 202:
            cid = r.json()["command_id"]
            if first_id and cid != first_id:
                raise SystemExit("a retried key produced a second command")
            first_id = cid
            if attempt:
                retried_same += 1
            accepted[cid] = f"{model}/{dataset}"
            now = time.monotonic()
            gaps.append(now - last_ok)
            last_ok = now
        break  # 202, 409 (in flight) or another definite answer
    else:
        gave_up += 1
    time.sleep(0.2)

deadline = time.monotonic() + SETTLE
final = {}
while time.monotonic() < deadline:
    pending = [c for c in accepted if final.get(c) not in ("succeeded", "dead", "cancelled")]
    if not pending:
        break
    for cid in pending:
        try:
            r = client.get(BASE + "/v1/commands/" + cid, headers=H)
            if r.status_code == 200:
                final[cid] = r.json()["state"]
                final[cid + ":run"] = (r.json().get("result") or {}).get("flow_run_id")
                final[cid + ":attempts"] = r.json().get("attempts")
        except httpx.HTTPError:
            pass
    time.sleep(2)
print("RESULT " + json.dumps({
    "accepted": len(accepted), "retried_same": retried_same, "transient": transient,
    "gave_up": gave_up, "max_gap": max(gaps or [0]), "codes": codes, "samples": samples,
    "states": {c: final.get(c) for c in accepted},
    "runs": {c: final.get(c + ":run") for c in accepted},
    "attempts": {c: final.get(c + ":attempts") for c in accepted},
}))
"""


def _run(*args: str, check: bool = True, timeout: float = 600, **kw) -> subprocess.CompletedProcess:
    out = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, **kw)
    if check and out.returncode != 0:
        raise AssertionError(f"{' '.join(args[:4])}…: {out.stderr[-3000:]}")
    return out


class Cluster:
    def __init__(self) -> None:
        self.name = f"exa-failover-{uuid.uuid4().hex[:6]}"
        self.dir = Path(tempfile.mkdtemp(prefix="exa-kind-"))
        self.kubeconfig = self.dir / "kubeconfig"
        self.env = {**os.environ, "KUBECONFIG": str(self.kubeconfig)}

    def kubectl(self, *args: str, check: bool = True, timeout: float = 300):
        return _run("kubectl", "-n", NS, *args, check=check, timeout=timeout, env=self.env)

    def apply(self, manifest: dict) -> None:
        path = self.dir / f"{uuid.uuid4().hex[:6]}.json"
        path.write_text(json.dumps(manifest))
        self.kubectl("apply", "-f", str(path))

    def control_plane_pods(self) -> list[str]:
        out = self.kubectl(
            "get", "pods", "-l", "app.kubernetes.io/component=control-plane",
            "--field-selector=status.phase=Running", "-o", "jsonpath={.items[*].metadata.name}",
        )  # fmt: skip
        return out.stdout.split()


@pytest.fixture(scope="module")
def cluster():
    for tool in ("kind", "kubectl", "helm", "docker"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is not installed")
    if _run("docker", "image", "inspect", IMAGE, check=False).returncode:
        pytest.skip(f"build {IMAGE} first (see the module docstring)")
    c = Cluster()
    try:
        _run(
            "kind", "create", "cluster", "--name", c.name, "--image", NODE,
            "--kubeconfig", str(c.kubeconfig), "--wait", "120s", timeout=400,
        )  # fmt: skip
        for image in (IMAGE, POSTGRES):
            _run("kind", "load", "docker-image", image, "--name", c.name, timeout=600)
        _run("kubectl", "create", "namespace", NS, env=c.env)
        _dependencies(c)
        _run(
            "helm", "upgrade", "--install", "rel", str(CHART), "-n", NS, "--wait", "--timeout", "6m",
            "--set", "global.imageRegistry=exa-kind-spire/",
            "--set", "controlPlane.image.tag=test",
            "--set", f"controlPlane.replicaCount={REPLICAS}",
            "--set", "controlPlane.env.PREFECT_API_URL=http://prefect:4200/api",
            "--set", "controlPlane.env.CONTROL_PLANE_RECONCILE_SECONDS=1",
            "--set", f"controlPlane.env.CONTROL_PLANE_COMMAND_LEASE_SECONDS={LEASE_SECONDS}",
            "--set", "controlPlane.env.RETRAIN_RATE_LIMIT_PER_MIN=100000",
            "--set", "dashboard.replicaCount=0",
            "--set", "agent.enabled=false",
            "--set", "ingress.enabled=false",
            env=c.env, timeout=500,
        )  # fmt: skip
        yield c
    finally:
        _run("kind", "delete", "cluster", "--name", c.name, check=False, timeout=300)


def _dependencies(c: Cluster) -> None:
    """Postgres, the Prefect stand-in, and the Secret."""
    password = secrets.token_hex(16)
    c.kubectl(
        "run", "postgres", "--image", POSTGRES, "--image-pull-policy", "IfNotPresent",
        "--port", "5432", "--env", "POSTGRES_USER=examlops", "--env", f"POSTGRES_PASSWORD={password}",
        "--env", "POSTGRES_DB=examlops", "--labels", "app=postgres",
    )  # fmt: skip
    c.kubectl("expose", "pod", "postgres", "--port", "5432")
    stub = c.dir / "prefect_stub.py"
    stub.write_text(PREFECT_STUB)
    c.kubectl("create", "configmap", "prefect-stub", f"--from-file={stub}")
    c.apply(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "prefect", "labels": {"app": "prefect"}},
            "spec": {
                "containers": [
                    {
                        "name": "prefect",
                        "image": IMAGE,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["python", "/stub/prefect_stub.py"],
                        "ports": [{"containerPort": 4200}],
                        "volumeMounts": [{"name": "stub", "mountPath": "/stub"}],
                    }
                ],
                "volumes": [{"name": "stub", "configMap": {"name": "prefect-stub"}}],
            },
        }
    )
    c.kubectl("expose", "pod", "prefect", "--port", "4200")
    for pod in ("postgres", "prefect"):
        c.kubectl("wait", "--for=condition=Ready", f"pod/{pod}", "--timeout=180s")
    dsn = f"postgresql://examlops:{password}@postgres:5432/examlops"
    c.kubectl(
        "create", "secret", "generic", "examlops-secrets",
        f"--from-literal=CONTROL_PLANE_TOKEN={secrets.token_hex(24)}",
        f"--from-literal=EXAMLOPS_POSTGRES_DSN={dsn}",
        f"--from-literal=AGENT_POSTGRES_DSN={dsn}",
    )  # fmt: skip


def _drill(c: Cluster) -> tuple[dict, list[str], dict]:
    """Run the load, kill replicas on schedule, and return the client's and the stub's view."""
    client = c.dir / "client.py"
    client.write_text(CLIENT)
    c.kubectl("create", "configmap", "drill-client", f"--from-file={client}")
    settle = LEASE_SECONDS * 3 + 60
    c.apply(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "drill"},
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "drill",
                        "image": IMAGE,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["python", "/drill/client.py"],
                        "env": [
                            {"name": "DURATION", "value": str(DURATION)},
                            {"name": "SETTLE", "value": str(settle)},
                            {
                                "name": "CONTROL_PLANE_TOKEN",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": "examlops-secrets",
                                        "key": "CONTROL_PLANE_TOKEN",
                                    }
                                },
                            },
                        ],
                        "volumeMounts": [{"name": "drill", "mountPath": "/drill"}],
                    }
                ],
                "volumes": [{"name": "drill", "configMap": {"name": "drill-client"}}],
            },
        }
    )
    c.kubectl("wait", "--for=condition=Ready", "pod/drill", "--timeout=120s")
    killed: list[str] = []

    def chaos() -> None:
        started = time.monotonic()
        for at in KILLS_AT:
            time.sleep(max(0.0, at - (time.monotonic() - started)))
            victim = _replica_mid_dispatch(c)
            if victim:
                _crash(c, victim)
                killed.append(victim)

    killer = threading.Thread(target=chaos)
    killer.start()
    c.kubectl(
        "wait", "--for=jsonpath={.status.phase}=Succeeded", "pod/drill",
        f"--timeout={DURATION + settle + 120}s", timeout=DURATION + settle + 180,
    )  # fmt: skip
    killer.join()
    logs = c.kubectl("logs", "drill").stdout
    result = json.loads(next(x for x in logs.splitlines() if x.startswith("RESULT "))[7:])
    stub = c.kubectl(
        "exec", "prefect", "--", "python", "-c",
        "import urllib.request;print(urllib.request.urlopen('http://localhost:4200/stub/runs').read().decode())",
    ).stdout  # fmt: skip
    return result, killed, json.loads(stub)


def _replica_mid_dispatch(c: Cluster, within: float = 30.0) -> str | None:
    """The control-plane pod that is waiting on Prefect right now, as the stub sees it."""
    deadline = time.monotonic() + within
    probe = "import urllib.request;print(urllib.request.urlopen('http://localhost:4200/stub/inflight').read().decode())"
    while time.monotonic() < deadline:
        ips = json.loads(c.kubectl("exec", "prefect", "--", "python", "-c", probe).stdout or "[]")
        if ips:
            pods = json.loads(
                c.kubectl("get", "pods", "-l", "app.kubernetes.io/component=control-plane",
                          "-o", "json").stdout
            )["items"]  # fmt: skip
            for pod in pods:
                if pod["status"].get("podIP") in ips:
                    return pod["metadata"]["name"]
        time.sleep(0.2)
    return None


def _crash(c: Cluster, pod: str) -> None:
    """SIGKILL the control plane's process in that pod, as a crash would: no shutdown hooks run.

    Deleting the pod is not a crash: the kubelet stops the container gracefully and the replica
    can finish its dispatch first. The kind node's container runtime can kill it outright.
    """
    node = f"{c.name}-control-plane"
    container = _run(
        "docker", "exec", node, "crictl", "ps", "-q", "--name", "control-plane",
        "--label", f"io.kubernetes.pod.name={pod}",
    ).stdout.split()[0]  # fmt: skip
    pid = _run(
        "docker", "exec", node, "crictl", "inspect", "-o", "go-template",
        "--template", "{{.info.pid}}", container,
    ).stdout.strip()  # fmt: skip
    _run("docker", "exec", node, "kill", "-9", pid)


@pytest.fixture(scope="module")
def drill(cluster):
    result, killed, stub = _drill(cluster)
    print(
        f"\nfailover drill: {result['accepted']} accepted, {result['transient']} transient "
        f"errors, {result['retried_same']} answers recovered by retry, max gap "
        f"{result['max_gap']:.1f}s, killed {killed}, create_flow_run calls {stub['calls']}, "
        f"answers {result['codes']}, second attempts "
        f"{sum(1 for n in result['attempts'].values() if (n or 0) > 1)}"
    )
    return {"result": result, "killed": killed, "stub": stub}


def test_replicas_were_actually_killed_under_load(drill):
    assert len(drill["killed"]) == len(KILLS_AT)
    assert drill["result"]["accepted"] >= 20, drill["result"]


def test_nothing_accepted_is_lost(drill):
    states = drill["result"]["states"]
    stuck = {c: s for c, s in states.items() if s != "succeeded"}
    assert not stuck, stuck
    assert drill["result"]["gave_up"] == 0


def test_nothing_runs_twice(drill):
    runs = drill["result"]["runs"]
    assert all(runs.values()), runs
    assert len(set(runs.values())) == len(runs)  # one flow run per command, none shared
    stub_runs = drill["stub"]["runs"]
    assert set(runs.values()) <= set(stub_runs)
    assert len(stub_runs) == len(runs)  # Prefect created no run no command asked for


def test_a_killed_replicas_dispatch_was_taken_over(drill):
    """Not vacuous: at least one kill caught a command mid-dispatch, and another replica finished
    it after the claim lease ran out: its dispatch reached Prefect again with the same key, or it
    needed a second attempt."""
    retried_at_prefect = drill["stub"]["calls"] > len(drill["stub"]["runs"])
    second_attempts = [c for c, n in drill["result"]["attempts"].items() if (n or 0) > 1]
    assert retried_at_prefect or second_attempts, (drill["stub"]["calls"], drill["result"])


def test_the_service_recovers_quickly(drill):
    # Longest wait for the next accepted submission, kills included. Generous: kube-proxy drops a
    # killed endpoint within seconds; the rest is the client's retry backoff.
    assert drill["result"]["max_gap"] < 30, drill["result"]["max_gap"]
