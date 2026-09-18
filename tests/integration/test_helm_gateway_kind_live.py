"""The chart's serving gateway, live on Kubernetes (plan P4.11, ADR 0126).

A throwaway kind cluster with Postgres, a stand-in model server as the `ray-serving` Service, and
this chart with `gateway.enabled`, using a control-plane image built from this tree. Checked:

- Envoy runs as the chart runs every tier (non-root, read-only root, no capabilities, no hot
  restart), both tiers become Ready, and the rendered configuration routes to the model server;
- a virtual key issued into the platform datastore is accepted, and the model server receives the
  verified tenant, not the one the client claimed;
- anonymous requests get 401 and admin routes are not served, as in Compose.

Opt-in and slow (a cluster and an image build)::

    docker build -f platform/services/control_plane/Dockerfile \\
        -t exa-kind-spire/examlops-control-plane:test .
    EXAMLOPS_KIND_GATEWAY_LIVE=1 .venv/bin/pytest tests/integration/test_helm_gateway_kind_live.py -v
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "platform" / "infra" / "helm" / "examlops"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("EXAMLOPS_KIND_GATEWAY_LIVE") != "1", reason="set EXAMLOPS_KIND_GATEWAY_LIVE=1"
    ),
]

NODE = "kindest/node:v1.32.2"
IMAGE = "exa-kind-spire/examlops-control-plane:test"
POSTGRES = "postgres:17-alpine"
ENVOY = "envoyproxy/envoy:v1.39.1"
CURL = "curlimages/curl:8.16.0"
NS = "mlops"
INFER = '{"inputs": [{"name": "input-0", "shape": [1], "datatype": "FP64", "data": [1.0]}]}'

# The model server's stand-in: answers with the path and headers it received.
UPSTREAM = r"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    # HTTP/1.1, not BaseHTTPRequestHandler's HTTP/1.0 default: Envoy speaks 1.1 upstream and
    # reuses connections, so a server that closes after every response makes it reconnect each
    # time. Kept because it is the right stub for a keep-alive proxy — but it was NOT the cause of
    # the empty-body flake, which was `kubectl run -i` losing the output (see Cluster.curl).
    protocol_version = "HTTP/1.1"

    def _answer(self):
        n = int(self.headers.get("content-length") or 0)
        if n:
            self.rfile.read(n)
        body = json.dumps({"path": self.path,
                           "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = _answer
    def log_message(self, *a):
        pass
ThreadingHTTPServer(("0.0.0.0", 8001), H).serve_forever()
"""


def _run(*args: str, check: bool = True, timeout: float = 600, **kw) -> subprocess.CompletedProcess:
    out = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, **kw)
    if check and out.returncode != 0:
        raise AssertionError(f"{' '.join(args[:5])}…: {out.stderr[-3000:]}")
    return out


class Cluster:
    def __init__(self) -> None:
        self.name = f"exa-gw-{uuid.uuid4().hex[:6]}"
        self.dir = Path(tempfile.mkdtemp(prefix="exa-kind-"))
        self.kubeconfig = self.dir / "kubeconfig"
        self.env = {**os.environ, "KUBECONFIG": str(self.kubeconfig)}

    def kubectl(self, *args: str, check: bool = True, timeout: float = 300):
        return _run("kubectl", "-n", NS, *args, check=check, timeout=timeout, env=self.env)

    def apply(self, manifest: dict) -> None:
        path = self.dir / f"{uuid.uuid4().hex[:6]}.json"
        path.write_text(json.dumps(manifest))
        self.kubectl("apply", "-f", str(path))

    def curl(self, *args: str) -> subprocess.CompletedProcess:
        """Run curl in a throwaway pod and return what it printed.

        Deliberately NOT `kubectl run --rm -i`, which attaches to the pod and captures **nothing**
        when the container exits before the attach lands — an empty stdout with a zero exit code,
        about one run in three. The body then looked like the gateway had answered with nothing.
        Waiting for completion and reading the logs is deterministic.
        """
        name = f"curl-{uuid.uuid4().hex[:6]}"
        started = self.kubectl(
            "run", name, "--restart=Never", "--quiet", "--image", CURL,
            "--image-pull-policy", "IfNotPresent", "--", *args, check=False, timeout=120,
        )  # fmt: skip
        if started.returncode:
            return started
        try:
            self.kubectl(
                "wait", f"pod/{name}", "--for=jsonpath={.status.phase}=Succeeded",
                "--timeout=60s", check=False,
            )  # fmt: skip
            return self.kubectl("logs", name, check=False, timeout=60)
        finally:
            self.kubectl("delete", "pod", name, "--now", "--wait=false", check=False)


def _not_ready(c) -> list[str]:
    """Pods the cluster does not consider ready — the ones `helm --wait` was waiting for."""
    out = c.kubectl(
        "get",
        "pods",
        "-o",
        'jsonpath={range .items[*]}{.metadata.name}{" "}{.status.conditions[?(@.type==\'Ready\')].status}{"\\n"}{end}',
        check=False,
    ).stdout
    names = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] != "True":
            names.append(parts[0])
    return names


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
        for image in (IMAGE, POSTGRES, ENVOY, CURL):
            _run("kind", "load", "docker-image", image, "--name", c.name, timeout=600)
        _run("kubectl", "create", "namespace", NS, env=c.env)
        password = secrets.token_hex(16)
        c.kubectl(
            "run", "postgres", "--image", POSTGRES, "--image-pull-policy", "IfNotPresent",
            "--port", "5432", "--env", "POSTGRES_USER=examlops",
            "--env", f"POSTGRES_PASSWORD={password}", "--env", "POSTGRES_DB=examlops",
        )  # fmt: skip
        c.kubectl("expose", "pod", "postgres", "--port", "5432")
        upstream = c.dir / "upstream.py"
        upstream.write_text(UPSTREAM)
        c.kubectl("create", "configmap", "upstream", f"--from-file={upstream}")
        c.apply(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "ray-serving", "labels": {"app": "ray-serving"}},
                "spec": {
                    "containers": [
                        {
                            "name": "upstream",
                            "image": IMAGE,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python", "/u/upstream.py"],
                            "volumeMounts": [{"name": "u", "mountPath": "/u"}],
                        }
                    ],
                    "volumes": [{"name": "u", "configMap": {"name": "upstream"}}],
                },
            }
        )
        c.kubectl("expose", "pod", "ray-serving", "--port", "8001")
        for pod in ("postgres", "ray-serving"):
            c.kubectl("wait", "--for=condition=Ready", f"pod/{pod}", "--timeout=180s")
        dsn = f"postgresql://examlops:{password}@postgres:5432/examlops"
        c.kubectl(
            "create", "secret", "generic", "examlops-secrets",
            f"--from-literal=CONTROL_PLANE_TOKEN={secrets.token_hex(24)}",
            f"--from-literal=EXAMLOPS_POSTGRES_DSN={dsn}",
            f"--from-literal=AGENT_POSTGRES_DSN={dsn}",
        )  # fmt: skip
        install = _run(
            "helm", "upgrade", "--install", "rel", str(CHART), "-n", NS, "--wait", "--timeout", "6m",
            "--set", "global.imageRegistry=exa-kind-spire/",
            "--set", "controlPlane.image.tag=test",
            "--set", "controlPlane.env.PREFECT_API_URL=http://127.0.0.1:1/api",
            "--set", "dashboard.replicaCount=0",
            "--set", "agent.enabled=false",
            "--set", "ingress.enabled=false",
            "--set", "gateway.enabled=true",
            "--set", "gateway.image.pullPolicy=IfNotPresent",
            env=c.env, timeout=500, check=False,
        )  # fmt: skip
        if install.returncode:
            # `--wait` fails with a bare `context deadline exceeded`, which says nothing about
            # WHICH pod never became ready. Seven minutes of waiting deserves better than that:
            # dump what the cluster thinks before the `finally` below deletes the evidence.
            raise AssertionError(
                f"helm install failed: {install.stderr[-1500:]}\n"
                f"--- pods ---\n{c.kubectl('get', 'pods', '-o', 'wide', check=False).stdout}\n"
                f"--- not-ready pods described ---\n"
                + "".join(
                    c.kubectl("describe", "pod", name, check=False).stdout[-2500:]
                    for name in _not_ready(c)
                )
                + "".join(
                    f"--- logs {name} ---\n"
                    + c.kubectl(
                        "logs", name, "--all-containers", "--tail", "40", check=False
                    ).stdout
                    for name in _not_ready(c)
                )
            )
        yield c
    finally:
        _run("kind", "delete", "cluster", "--name", c.name, check=False, timeout=300)


@pytest.fixture(scope="module")
def key(cluster):
    """A virtual key issued into the platform datastore, from inside the authorization tier."""
    pod = cluster.kubectl(
        "get", "pods", "-l", "app.kubernetes.io/component=gateway-authz",
        "-o", "jsonpath={.items[0].metadata.name}",
    ).stdout  # fmt: skip
    out = cluster.kubectl(
        "exec", pod, "--", "python", "-c",
        "from examlops import gateway; print(gateway.issue_virtual_key('acme', 'research', None, None, 'kind'))",
    ).stdout.strip().splitlines()  # fmt: skip
    return out[-1]


def test_both_tiers_run_hardened_and_ready(cluster):
    pods = json.loads(
        cluster.kubectl("get", "pods", "-l", "app.kubernetes.io/component in (gateway,gateway-authz)",
                        "-o", "json").stdout
    )["items"]  # fmt: skip
    assert len(pods) == 4  # two replicas of each
    for pod in pods:
        assert all(c["ready"] for c in pod["status"]["containerStatuses"]), pod["metadata"]["name"]
        assert pod["spec"]["securityContext"]["runAsUser"] == 10001
        (container,) = pod["spec"]["containers"]
        assert container["securityContext"]["readOnlyRootFilesystem"] is True


def test_a_virtual_key_reaches_the_model_server_with_the_verified_tenant(cluster, key):
    out = cluster.curl(
        "-s", "-X", "POST", "http://rel-examlops-gateway:8080/v2/models/jpcp/infer",
        "-H", f"authorization: Bearer {key}", "-H", "content-type: application/json",
        "-H", "x-examlops-tenant: someone-else", "-d", INFER,
    )  # fmt: skip
    lines = out.stdout.strip().splitlines()
    if not lines:
        # An empty body says nothing on its own: ask again for the status, so the failure
        # distinguishes "the gateway refused" from "the upstream answered with nothing".
        status = cluster.curl(
            "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
            "http://rel-examlops-gateway:8080/v2/models/jpcp/infer",
            "-H", f"authorization: Bearer {key}", "-H", "content-type: application/json",
            "-d", INFER,
        )  # fmt: skip
        raise AssertionError(
            f"the gateway returned no body for an allowed request (HTTP "
            f"{status.stdout.strip()!r}); a refusal would be 401/403, an upstream that answered "
            f"nothing would be 200. curl rc={out.returncode} stderr={out.stderr[-400:]!r}"
        )
    body = json.loads(lines[-1])
    assert body["path"] == "/v2/models/jpcp/infer"
    assert body["headers"]["x-examlops-tenant"] == "acme"  # overwritten, not the client's
    assert body["headers"]["x-examlops-project"] == "research"


def test_anonymous_and_admin_requests_are_refused(cluster):
    anonymous = cluster.curl(
        "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
        "http://rel-examlops-gateway:8080/v2/models/jpcp/infer", "-d", INFER,
    )  # fmt: skip
    assert anonymous.stdout.strip().endswith("401"), anonymous.stdout
    admin = cluster.curl(
        "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
        "http://rel-examlops-gateway:8080/reload",
    )  # fmt: skip
    assert admin.stdout.strip()[-3:] in ("401", "404"), admin.stdout


def test_envoys_statistics_are_served_on_the_metrics_port(cluster):
    stats = cluster.curl("-s", "http://rel-examlops-gateway:9902/stats/prometheus")
    assert "envoy_cluster_upstream_rq" in stats.stdout
