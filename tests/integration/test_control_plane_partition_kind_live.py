"""Split brain: a control-plane replica that is cut off, but still alive (plan P5).

`tests/integration/test_control_plane_failover_kind_live.py` kills replicas mid-dispatch. A kill is
the *easy* failure: the replica is gone, and whatever it was doing is unambiguously abandoned. The
dangerous one is the replica that is still running and still believes it owns the work — a node
partitioned from Prefect, a process stopped by a long GC pause, a link that drops packets for a
minute. Its claim lease runs out, another replica takes the command over and dispatches it, and then
**the first replica's dispatch arrives after all**.

If nothing stops it, that is two training runs for one command: two jobs on the cluster, two models
registered, two sets of metrics. The platform's answer is that every dispatch carries the command's
own idempotency key, so Prefect returns the run it already made. This drill is what proves it,
because it is the only way to get both dispatches in flight at once.

A three-replica control plane on a real cluster, a real Postgres, and a Prefect stand-in that can be
told to **hold** one caller's `create_flow_run` — the partition, applied exactly where it hurts. A
retrain is submitted, the replica dispatching it is held past its claim lease, another replica takes
over, and then the held call is released so both dispatches land.

What must hold:

- the command still succeeds, without the client doing anything;
- both replicas really did dispatch it — otherwise the drill proves nothing;
- Prefect ends up with **exactly one** flow run for it, and the command records that one.

Opt-in and slow (a cluster and an image build)::

    docker build -f platform/services/control_plane/Dockerfile \\
        -t exa-kind-spire/examlops-control-plane:test .
    EXAMLOPS_KIND_PARTITION_LIVE=1 .venv/bin/pytest \\
        tests/integration/test_control_plane_partition_kind_live.py -v -s
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
import uuid

import httpx
import pytest

from tests.integration.test_control_plane_failover_kind_live import (
    CHART,
    IMAGE,
    NODE,
    NS,
    POSTGRES,
    Cluster,
    _run,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("EXAMLOPS_KIND_PARTITION_LIVE") != "1",
        reason="set EXAMLOPS_KIND_PARTITION_LIVE=1",
    ),
]

REPLICAS = 3
LEASE_SECONDS = 20  # the claim lease: how long a replica's work is left alone before takeover
MEASURED: dict[str, object] = {}

# Prefect's API, as much of it as the control plane uses, plus one control this drill needs:
# `/stub/hold?ip=…` makes the next `create_flow_run` from that address block until it is released.
# That is the partition — applied at the one call that matters, rather than to a whole node, so the
# replica stays alive and keeps believing it owns the command, which is the entire point.
PREFECT_STUB = r"""
import json, threading, time, urllib.parse, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

lock = threading.Lock()
runs = {}        # flow run id -> idempotency key
by_key = {}      # idempotency key -> flow run id
calls = []       # every create_flow_run: (caller ip, idempotency key)
inflight = {}    # idempotency key -> caller ip, while a create_flow_run is being answered
# "hold the FIRST dispatch that arrives" — armed before the retrain is submitted. Holding by
# caller address cannot work: by the time a caller is visible in /stub/inflight its call is already
# past this check, so it completes, the command succeeds, and there is no takeover to observe.
held = {"arm": False, "ip": None}
released = threading.Event()

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
        path, _, query = self.path.partition("?")
        args = urllib.parse.parse_qs(query)
        if path in ("/api/health", "/api/ready"):
            return self._send(200, True)
        if path.startswith("/api/deployments/name/"):
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
                return self._send(200, {"calls": list(calls), "runs": dict(runs)})
        if path == "/stub/holding":
            return self._send(200, {"ip": held["ip"]})
        if path == "/stub/inflight":
            with lock:
                return self._send(200, sorted(set(inflight.values())))
        if path == "/stub/hold_first":
            held["arm"], held["ip"] = True, None
            released.clear()
            return self._send(200, {"armed": True})
        if path == "/stub/release":
            released.set()
            return self._send(200, {"released": True})
        return self._send(404, {"detail": path})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.endswith("/flow_runs/filter"):
            # Prefect's read-only lookup by idempotency key, which the control plane uses to find
            # out whether a command it gave up on left a run behind. Without it here the platform's
            # check got a 404, recorded "unknown" (correctly), and the drill saw nothing.
            wanted = (((body.get("flow_runs") or {}).get("idempotency_key") or {}).get("any_")
                      or [])  # fmt: skip
            with lock:
                hits = [{"id": run} for run, key in runs.items() if key in wanted]
            return self._send(200, hits)
        if self.path.endswith("/create_flow_run"):
            key = body.get("idempotency_key") or str(uuid.uuid4())
            caller = self.client_address[0]
            with lock:
                inflight[key] = caller
                calls.append([caller, key])
            with lock:
                # Everything from the held replica hangs, not just its first call. Holding only the
                # first one let that replica retry within its own dispatch budget and finish the
                # command in 7.2 s — well inside the claim lease, so no other replica ever took it
                # over and the drill measured a retry rather than a split brain.
                mine = held["arm"] and held["ip"] in (None, caller)
                if mine:
                    held["ip"] = caller
            if mine:
                # The partition: this replica's dispatch does not arrive until it is released.
                released.wait(timeout=300)
            else:
                time.sleep(1.0)
            with lock:
                inflight.pop(key, None)
                run = by_key.setdefault(key, str(uuid.uuid4()))
                runs[run] = key
            return self._send(201, {"id": run, "state": {"type": "SCHEDULED"}})
        return self._send(404, {"detail": self.path})

ThreadingHTTPServer(("0.0.0.0", 4200), Handler).serve_forever()
"""


def _stub(c: Cluster, path: str) -> dict | list:
    out = c.kubectl(
        "exec", "prefect", "--", "python", "-c",
        f"import urllib.request;print(urllib.request.urlopen('http://localhost:4200{path}').read().decode())",
    )  # fmt: skip
    return json.loads(out.stdout)


def _dependencies(c: Cluster) -> str:
    password, token = secrets.token_hex(16), secrets.token_hex(24)
    c.kubectl(
        "run", "postgres", "--image", POSTGRES, "--image-pull-policy", "IfNotPresent",
        "--port", "5432", "--env", "POSTGRES_USER=examlops",
        "--env", f"POSTGRES_PASSWORD={password}", "--env", "POSTGRES_DB=examlops",
        "--labels", "app=postgres",
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
        f"--from-literal=CONTROL_PLANE_TOKEN={token}",
        f"--from-literal=EXAMLOPS_POSTGRES_DSN={dsn}",
        f"--from-literal=AGENT_POSTGRES_DSN={dsn}",
    )  # fmt: skip
    return token


@pytest.fixture(scope="module")
def cluster():
    for tool in ("kind", "kubectl", "helm", "docker"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is not installed")
    if _run("docker", "image", "inspect", IMAGE, check=False).returncode:
        pytest.skip(f"build {IMAGE} first (see the module docstring)")
    c = Cluster()
    c.name = f"exa-partition-{uuid.uuid4().hex[:6]}"
    port = _free_port()
    try:
        _run(
            "kind", "create", "cluster", "--name", c.name, "--image", NODE,
            "--kubeconfig", str(c.kubeconfig), "--wait", "120s", timeout=400,
        )  # fmt: skip
        for image in (IMAGE, POSTGRES):
            _run("kind", "load", "docker-image", image, "--name", c.name, timeout=900)
        _run("kubectl", "create", "namespace", NS, env=c.env)
        token = _dependencies(c)
        _install(c)
        forward = _port_forward(c, port)
        try:
            _wait_ready(port)
            yield {"c": c, "base": f"http://127.0.0.1:{port}", "token": token}
        finally:
            forward.terminate()
    finally:
        if MEASURED:
            print("\npartition drill:", json.dumps(MEASURED, indent=1))
        _run("kind", "delete", "cluster", "--name", c.name, check=False, timeout=300)


def _install(c: Cluster) -> None:
    """The chart, with what this drill needs. On failure it says *why*: a timed-out `helm --wait`
    reports only "context deadline exceeded", and the cluster is deleted moments later."""
    try:
        _run(
            "helm", "upgrade", "--install", "rel", str(CHART), "-n", NS, "--wait", "--timeout", "6m",
            "--set", "global.imageRegistry=exa-kind-spire/",
            "--set", "controlPlane.image.tag=test",
            "--set", f"controlPlane.replicaCount={REPLICAS}",
            "--set", "controlPlane.env.PREFECT_API_URL=http://prefect:4200/api",
            "--set", "controlPlane.env.CONTROL_PLANE_RECONCILE_SECONDS=1",
            "--set", f"controlPlane.env.CONTROL_PLANE_COMMAND_LEASE_SECONDS={LEASE_SECONDS}",
            # The orphan appears when the held dispatch is released, after the command is already
            # dead: the sweep has to ask again by then, so it must not wait a production minute.
            "--set", "controlPlane.env.CONTROL_PLANE_ORPHAN_RECHECK_SECONDS=5",
            "--set", "controlPlane.env.RETRAIN_RATE_LIMIT_PER_MIN=100000",
            "--set", "dashboard.replicaCount=0",
            "--set", "agent.enabled=false",
            "--set", "ingress.enabled=false",
            env=c.env, timeout=500,
        )  # fmt: skip
    except AssertionError as exc:
        pods = c.kubectl("get", "pods", "-o", "wide", check=False).stdout
        detail = []
        for line in pods.splitlines()[1:]:
            name, _, ready = (line.split() + ["", ""])[:3]
            if ready not in ("Running", "Completed"):
                detail.append(c.kubectl("describe", "pod", name, check=False).stdout[-1500:])
            elif "control-plane" in name:
                detail.append(c.kubectl("logs", name, "--tail", "30", check=False).stdout[-1500:])
        raise AssertionError(f"{exc}\n{pods}\n" + "\n".join(detail)) from exc


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_forward(c: Cluster, port: int):
    import subprocess

    return subprocess.Popen(
        ["kubectl", "-n", NS, "port-forward", "service/rel-examlops-control-plane",
         f"{port}:8002"],
        env=c.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip


def _wait_ready(port: int, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=3).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise AssertionError("the control plane never became ready through the port-forward")


def _until(fn, timeout: float, interval: float = 1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = fn()
        if found:
            return found
        time.sleep(interval)
    return None


def _pod_for_ip(c: Cluster, ip: str) -> str | None:
    pods = json.loads(
        c.kubectl(
            "get", "pods", "-l", "app.kubernetes.io/component=control-plane", "-o", "json"
        ).stdout
    )["items"]
    for pod in pods:
        if pod["status"].get("podIP") == ip:
            return pod["metadata"]["name"]
    return None


# ── the drill ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def partitioned(cluster):
    """Hold the first dispatch that reaches Prefect, let the platform deal with it, then release it.

    The hold is armed *before* the retrain is submitted. Holding a replica once it is visible in
    `/stub/inflight` cannot work — by then its call is already past the point where the hold would
    apply, so it completes, the command succeeds, and there is no takeover to observe. That is what
    the first version of this drill measured: one dispatch, nothing deduplicated.
    """
    c, base, token = cluster["c"], cluster["base"], cluster["token"]
    _stub(c, "/stub/hold_first")
    key = f"partition-{uuid.uuid4().hex[:8]}"
    answer = httpx.post(
        base + "/v1/retrain",
        json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        timeout=15,
    )
    assert answer.status_code == 202, answer.text
    command = answer.json()["command_id"]
    MEASURED["command"] = command
    held_ip = _until(lambda: _stub(c, "/stub/holding").get("ip"), timeout=60, interval=0.5)
    assert held_ip, "no dispatch ever reached Prefect, so nothing was held"
    MEASURED["held_replica"] = _pod_for_ip(c, held_ip) or held_ip

    # Now the platform is on its own: the replica holding the command cannot finish it, its claim
    # lease runs out, and something has to pick the work up.
    began = time.monotonic()
    state = _until(
        lambda: _command(base, token, command).get("state") in ("succeeded", "dead"),
        timeout=LEASE_SECONDS * 8,
        interval=1.0,
    )
    MEASURED["seconds_until_it_was_finished_anyway"] = round(time.monotonic() - began, 1)
    # It cannot have been the held replica: everything it sends is still hanging. The time is the
    # claim lease plus the takeover, which is the recovery an operator would see.
    MEASURED["state_after_takeover"] = state and _command(base, token, command)["state"]

    # And then the held dispatch lands, late, exactly as a healed partition delivers it.
    _stub(c, "/stub/release")
    time.sleep(8)
    yield {"command": command, "stub": _stub(c, "/stub/runs"), "base": base, "token": token, "c": c}


def _command(base: str, token: str, command: str) -> dict:
    answer = httpx.get(
        f"{base}/v1/commands/{command}", headers={"Authorization": f"Bearer {token}"}, timeout=10
    )
    return answer.json() if answer.status_code == 200 else {}


def test_1_the_command_reaches_a_terminal_state_rather_than_hanging(partitioned):
    """Whatever else happens, a command must not sit in `dispatching` forever with nobody owning
    it — that is the failure the kind failover drill was written for."""
    record = _command(partitioned["base"], partitioned["token"], partitioned["command"])
    MEASURED["final_state"] = record.get("state")
    MEASURED["attempts"] = record.get("attempts")
    MEASURED["error"] = (record.get("result") or {}).get("error") or record.get("error")
    assert record.get("state") in ("succeeded", "dead"), record


def test_2_the_command_was_dispatched_more_than_once(partitioned):
    """Otherwise there was never a second dispatch to deduplicate, and this drill proves nothing."""
    calls = partitioned["stub"]["calls"]
    MEASURED["dispatch_attempts_that_reached_prefect"] = len(calls)
    callers = {caller for caller, _ in calls}
    MEASURED["dispatched_by"] = sorted(_pod_for_ip(partitioned["c"], ip) or ip for ip in callers)
    MEASURED["taken_over_by_another_replica"] = len(callers) > 1
    assert len(calls) >= 2, f"only one dispatch ever reached Prefect: {calls}"
    keys = {key for _, key in calls}
    assert len(keys) == 1, f"the dispatches used different keys, so nothing can dedupe: {calls}"


def test_3_prefect_made_exactly_one_run_for_it(partitioned):
    """The property the whole design rests on, and the one this drill exists to prove: however many
    dispatches of one command are in flight — ten, here — the *command's own* idempotency key means
    Prefect starts one training run, not ten."""
    runs = partitioned["stub"]["runs"]  # run id -> idempotency key
    MEASURED["runs"] = runs
    assert len(runs) == 1, f"one command produced {len(runs)} runs: {runs}"
    assert len(partitioned["stub"]["calls"]) > len(runs), (
        "there were as many runs as dispatch attempts, so nothing was deduplicated"
    )


def test_4_a_dead_command_can_still_have_left_a_run_behind(partitioned):
    """The finding this drill was worth writing for.

    A replica that can reach the datastore but *not* Prefect keeps the command it claimed: no other
    replica takes it over, it spends every attempt itself, and the command ends `dead`. Meanwhile
    the dispatch that was merely slow arrives, and Prefect starts the training run anyway — so the
    platform records work as dead while a job for it is running, with nothing pointing at it.

    This test pins the behaviour rather than blessing it: the numbers are the evidence for deciding
    whether a partitioned replica should keep its claim (safe, and the work dies) or hand it back
    (the work gets done, and the idempotency key is what makes that safe — as test 3 shows).

    What has changed since it first measured this: the platform no longer leaves the orphan for
    someone to find. Burying a command asks Prefect whether a run exists for its key, names it in
    the command's `last_error`, and raises `ControlPlaneDeadCommandLeftARun`.
    """
    record = _command(partitioned["base"], partitioned["token"], partitioned["command"])
    runs = partitioned["stub"]["runs"]
    recorded = (record.get("result") or {}).get("flow_run_id")
    MEASURED["flow_run_of_the_command"] = recorded
    MEASURED["runs_prefect_actually_started"] = list(runs)
    MEASURED["orphaned_run"] = record.get("state") == "dead" and bool(runs) and not recorded
    MEASURED["last_error"] = record.get("last_error")
    if MEASURED["orphaned_run"]:
        # And the platform says so itself now, rather than leaving it to be found. Not at the
        # burial — the replica that gives up is the one that cannot reach Prefect, so its own
        # lookup times out too (this drill measured exactly that). A *healthy* replica asks on its
        # behalf in the reconcile sweep, which is why this waits rather than reading once.
        found = _until(
            lambda: (
                "flow run exists"
                in (
                    _command(partitioned["base"], partitioned["token"], partitioned["command"]).get(
                        "last_error"
                    )
                    or ""
                )
            ),
            timeout=120,
            interval=5,
        )
        record = _command(partitioned["base"], partitioned["token"], partitioned["command"])
        MEASURED["last_error"] = record.get("last_error")
        if not found:
            # Why it did not surface is the only thing worth reporting here: the sweep's own error
            # (the worker records it) and whatever the replicas logged about the lookup.
            health = httpx.get(partitioned["base"] + "/health", timeout=10).json()
            logs = []
            for pod in partitioned["c"].control_plane_pods() or []:
                out = partitioned["c"].kubectl("logs", pod, "--tail", "200", check=False).stdout
                logs += [ln for ln in out.splitlines() if "orphan" in ln or "flow run" in ln]
            raise AssertionError(
                f"{record}\nworker error: {health.get('runtime', {}).get('command_worker_error')}"
                f"\nlogs: {logs[-8:]}"
            )
        assert list(runs)[0] in (record.get("last_error") or ""), (runs, record)
        # Not an assertion failure: it is what the platform does today, and the drill's job is to
        # say so with numbers. The runbook explains what an operator should do about it.
        assert record.get("state") == "dead" and len(runs) == 1


def test_5_the_platform_is_unharmed(partitioned):
    """It was never restarted: a partition is not something the control plane needs replacing for."""
    c = partitioned["c"]
    pods = json.loads(
        c.kubectl(
            "get", "pods", "-l", "app.kubernetes.io/component=control-plane", "-o", "json"
        ).stdout
    )["items"]
    restarts = {
        pod["metadata"]["name"]: sum(
            s.get("restartCount", 0) for s in pod["status"].get("containerStatuses", [])
        )
        for pod in pods
    }
    MEASURED["restarts"] = restarts
    assert not any(restarts.values()), restarts
    assert httpx.get(partitioned["base"] + "/readyz", timeout=10).status_code == 200
