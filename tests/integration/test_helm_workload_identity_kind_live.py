"""On Kubernetes, the chart's tiers get their SPIFFE identities from a real SPIRE (ADR 0125).

A throwaway kind cluster runs SPIRE's own hardened charts (server, node agent, SPIFFE CSI driver,
spire-controller-manager), the way a site installs it, and a Postgres for the control plane. Then
this chart is installed with ``workloadIdentity.enabled``, using a control-plane image built from
this tree. Checked against the running system:

- the controller manager turns the chart's ClusterSPIFFEIDs into registrations, and the dashboard
  pod's spiffe-helper, reaching the Workload API through the CSI driver as a non-root user with a
  read-only root, keeps a JWT-SVID naming the dashboard's SPIFFE ID;
- the real control plane accepts that token, which also proves its own helper keeps the trust
  bundle; it allows only the scopes the chart mapped (here `read`, so an admin change is refused),
  and counts the request as a `workload` authentication;
- a pod of another release, with the same name and tier labels, gets no tier identity: only
  SPIRE's own fallback identity covers it.

The chart's default ``clusterSPIFFEID.className`` has to be the hardened chart's controller-manager
class (``spire-mgmt-spire`` for release ``spire`` in ``spire-mgmt``): the controller manager
ignores any other class silently, which is how the first run of this test failed.

The dashboard image is not needed: an ephemeral container (the control-plane image, which has
python and `examlops`) mounts the pod's SVID volume and makes the calls.

Opt-in and slow (a cluster, SPIRE, an image build)::

    docker build -f platform/services/control_plane/Dockerfile \\
        -t exa-kind-spire/examlops-control-plane:test .
    EXAMLOPS_KIND_SPIRE_LIVE=1 .venv/bin/pytest \\
        tests/integration/test_helm_workload_identity_kind_live.py -v
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "platform" / "infra" / "helm" / "examlops"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("EXAMLOPS_KIND_SPIRE_LIVE") != "1", reason="set EXAMLOPS_KIND_SPIRE_LIVE=1"
    ),
]

HARDENED = "https://spiffe.github.io/helm-charts-hardened/"
SPIRE_CRDS, SPIRE_CHART = "0.6.1", "0.30.2"
NODE = "kindest/node:v1.32.2"
IMAGE = "exa-kind-spire/examlops-control-plane:test"
POSTGRES = "postgres:17-alpine"
DOMAIN = "example.org"
NS = "mlops"
DASHBOARD_ID = f"spiffe://{DOMAIN}/ns/{NS}/rel-examlops/dashboard"


def _run(*args: str, check: bool = True, timeout: float = 600, **kw) -> subprocess.CompletedProcess:
    out = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, **kw)
    if check and out.returncode != 0:
        raise AssertionError(f"{' '.join(args[:4])}…: {out.stderr[-3000:]}")
    return out


def _until(fn, timeout: float = 300.0, every: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = fn()
        if found:
            return found
        time.sleep(every)
    return None


class Cluster:
    def __init__(self) -> None:
        self.name = f"exa-spire-{uuid.uuid4().hex[:6]}"
        self.kubeconfig = Path(tempfile.mkdtemp(prefix="exa-kind-")) / "kubeconfig"
        self.env = {**os.environ, "KUBECONFIG": str(self.kubeconfig)}

    def kubectl(self, *args: str, check: bool = True, timeout: float = 300):
        return _run("kubectl", *args, check=check, timeout=timeout, env=self.env)

    def helm(self, *args: str, timeout: float = 900):
        return _run("helm", *args, timeout=timeout, env=self.env)

    def pod(self, component: str) -> str:
        selector = f"app.kubernetes.io/instance=rel,app.kubernetes.io/component={component}"
        out = self.kubectl(
            "-n", NS, "get", "pods", "-l", selector,
            "-o", "jsonpath={.items[0].metadata.name}", check=False,
        )  # fmt: skip
        return out.stdout.strip()

    def spire_server(self, *args: str) -> str:
        """Run the SPIRE server CLI, wherever the hardened chart put the server."""
        where = self.kubectl(
            "get", "pods", "-A", "-l", "app.kubernetes.io/name=server,app.kubernetes.io/instance=spire",
            "-o", "jsonpath={.items[0].metadata.namespace} {.items[0].metadata.name}",
        ).stdout.split()  # fmt: skip
        return self.kubectl(
            "-n", where[0], "exec", where[1], "-c", "spire-server", "--",
            "/opt/spire/bin/spire-server", *args, check=False,
        ).stdout  # fmt: skip

    def helper_has_svid(self, component: str) -> bool:
        pod = self.pod(component)
        if not pod:
            return False
        logs = self.kubectl("-n", NS, "logs", pod, "-c", "spiffe-helper", check=False).stdout
        return "SVID updated" in logs or "bundle updated" in logs.lower()

    def debug_python(self, pod: str, target: str, code: str) -> subprocess.CompletedProcess:
        """Run python in an ephemeral container that mounts the pod's SVID volume."""
        spec = {
            "volumeMounts": [
                {"name": "spiffe-svid", "mountPath": "/run/spire/svid", "readOnly": True}
            ],
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
        }
        custom = self.kubeconfig.parent / f"debug-{uuid.uuid4().hex[:6]}.json"
        custom.write_text(json.dumps(spec))
        name = f"probe-{uuid.uuid4().hex[:6]}"
        self.kubectl(
            "-n", NS, "debug", pod, "--image", IMAGE, "--image-pull-policy", "IfNotPresent",
            "--container", name, "--target", target, "--profile", "restricted",
            "--custom", str(custom), "--", "python", "-c", code,
        )  # fmt: skip
        _until(
            lambda: '"terminated"' in self.kubectl(
                "-n", NS, "get", "pod", pod, "-o",
                f"jsonpath={{.status.ephemeralContainerStatuses[?(@.name=='{name}')].state}}",
                check=False,
            ).stdout,
            timeout=180,
        )  # fmt: skip
        return self.kubectl("-n", NS, "logs", pod, "-c", name, check=False)


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
        # SPIRE the way a site installs it: its own hardened charts.
        c.helm(
            "upgrade", "--install", "-n", "spire-mgmt", "--create-namespace", "spire-crds",
            "spire-crds", "--repo", HARDENED, "--version", SPIRE_CRDS, "--wait",
        )  # fmt: skip
        c.helm(
            "upgrade", "--install", "-n", "spire-mgmt", "spire", "spire", "--repo", HARDENED,
            "--version", SPIRE_CHART, "--wait", "--timeout", "10m",
            "--set", "global.spire.namespaces.create=true",
            "--set", f"global.spire.trustDomain={DOMAIN}",
            "--set", "global.spire.clusterName=kind",
            "--set", "spiffe-oidc-discovery-provider.enabled=false",
        )  # fmt: skip
        _platform(c)
        yield c
    finally:
        _run("kind", "delete", "cluster", "--name", c.name, check=False, timeout=300)


def _platform(c: Cluster) -> None:
    """A Postgres, the Secret, and this chart with workload identities on."""
    c.kubectl("create", "namespace", NS)
    password = secrets.token_hex(16)
    c.kubectl(
        "-n", NS, "run", "postgres", "--image", POSTGRES, "--image-pull-policy", "IfNotPresent",
        "--port", "5432", "--env", "POSTGRES_USER=examlops", "--env", f"POSTGRES_PASSWORD={password}",
        "--env", "POSTGRES_DB=examlops", "--labels", "app=postgres",
    )  # fmt: skip
    c.kubectl("-n", NS, "expose", "pod", "postgres", "--port", "5432")
    c.kubectl("-n", NS, "wait", "--for=condition=Ready", "pod/postgres", "--timeout=180s")
    dsn = f"postgresql://examlops:{password}@postgres:5432/examlops"
    c.kubectl(
        "-n", NS, "create", "secret", "generic", "examlops-secrets",
        f"--from-literal=CONTROL_PLANE_TOKEN={secrets.token_hex(24)}",
        f"--from-literal=EXAMLOPS_POSTGRES_DSN={dsn}",
        f"--from-literal=AGENT_POSTGRES_DSN={dsn}",
    )  # fmt: skip
    c.helm(
        "upgrade", "--install", "rel", str(CHART), "-n", NS, "--timeout", "5m",
        "--set", "global.imageRegistry=exa-kind-spire/",
        "--set", "controlPlane.image.tag=test",
        # The dashboard tier runs the control-plane image: only its identity sidecar is under
        # test, and the ephemeral probe container makes the calls.
        "--set", "dashboard.image.repository=examlops-control-plane",
        "--set", "dashboard.image.tag=test",
        "--set", "dashboard.replicaCount=1",
        "--set", "agent.enabled=false",
        "--set", "ingress.enabled=false",
        "--set", "workloadIdentity.enabled=true",
        "--set", f"workloadIdentity.trustDomain={DOMAIN}",
        # Read only: `write` covers every action scope, so the default mapping could not show the
        # control plane refusing what a workload was not given.
        "--set", "workloadIdentity.callers.dashboard.scopes={read}",
    )  # fmt: skip


def test_the_controller_manager_registers_each_tier(cluster):
    ids = json.loads(cluster.kubectl("get", "clusterspiffeids", "-o", "json").stdout)["items"]
    ours = {i["metadata"]["name"]: i for i in ids if "rel-examlops" in i["metadata"]["name"]}
    assert set(ours) == {f"{NS}-rel-examlops-control-plane", f"{NS}-rel-examlops-dashboard"}
    # The chart's default className must be the hardened chart's controller-manager class, which
    # ignores every other class silently: no status, no entry, no token.
    assert _until(lambda: DASHBOARD_ID in cluster.spire_server("entry", "show"), timeout=240), (
        cluster.kubectl("get", "clusterspiffeids", "-o", "yaml").stdout[-3000:]
    )


def test_the_dashboards_helper_keeps_its_svid(cluster):
    assert _until(lambda: cluster.helper_has_svid("dashboard"), timeout=360)
    probe = """
import json, pathlib, time, jwt
token = pathlib.Path('/run/spire/svid/control-plane.jwt').read_text().strip()
claims = jwt.decode(token, options={'verify_signature': False})
print(json.dumps({'sub': claims['sub'], 'aud': claims['aud'], 'ttl': claims['exp'] - time.time()}))
"""
    out = cluster.debug_python(cluster.pod("dashboard"), "dashboard", probe)
    claims = json.loads(out.stdout.strip().splitlines()[-1])
    assert claims["sub"] == DASHBOARD_ID
    assert claims["aud"] == ["control-plane"] and claims["ttl"] <= 5 * 60 + 30


def test_the_control_plane_acts_on_it_with_only_the_mapped_scopes(cluster):
    assert _until(lambda: cluster.helper_has_svid("control-plane"), timeout=360)  # the bundle
    cp = cluster.pod("control-plane")
    ip = cluster.kubectl("-n", NS, "get", "pod", cp, "-o", "jsonpath={.status.podIP}").stdout
    probe = f"""
import os, time, httpx
os.environ['CONTROL_PLANE_TOKEN_FILE'] = '/run/spire/svid/control-plane.jwt'
from examlops import service_auth
base = 'http://{ip.strip()}:8002'
def auth():
    return {{'Authorization': 'Bearer ' + service_auth.control_plane_bearer(None)}}
for _ in range(60):
    try:
        read = httpx.get(base + '/v1/commands', headers=auth())
        if read.status_code != 503:
            break
    except httpx.HTTPError:
        pass
    time.sleep(3)
admin = httpx.put(base + '/v1/modelzoo/config', json={{'auto_retrain': False}}, headers=auth())
counted = [line for line in httpx.get(base + '/metrics').text.splitlines()
           if line.startswith('control_plane_authentications_total{{') and 'dashboard' in line]
print(read.status_code, admin.status_code, counted)
"""
    out = cluster.debug_python(cluster.pod("dashboard"), "dashboard", probe)
    last = out.stdout.strip().splitlines()[-1]
    read_status, admin_status, counted = last.split(" ", 2)
    assert read_status == "200", out.stdout + out.stderr
    assert admin_status == "403", last  # mapped to `read` only in this install
    assert 'method="workload"' in counted and 'principal="dashboard"' in counted, counted


def test_a_pod_of_another_release_gets_no_tier_identity(cluster):
    """Same name and component labels, another release: the selectors must not match it."""
    cluster.kubectl(
        "-n", NS, "run", "stranger", "--image", IMAGE, "--image-pull-policy", "IfNotPresent",
        "--labels",
        "app.kubernetes.io/name=examlops,app.kubernetes.io/instance=other,"
        "app.kubernetes.io/component=dashboard",
        "--command", "--", "sleep", "600",
    )  # fmt: skip
    cluster.kubectl("-n", NS, "wait", "--for=condition=Ready", "pod/stranger", "--timeout=120s")
    uid = cluster.kubectl("-n", NS, "get", "pod", "stranger", "-o", "jsonpath={.metadata.uid}")
    selector = f"k8s:pod-uid:{uid.stdout.strip()}"
    # The controller manager registers pods by UID; SPIRE's fallback identity still covers it.
    assert _until(
        lambda: "Entry ID" in cluster.spire_server("entry", "show", "-selector", selector),
        timeout=120,
    )
    entries = cluster.spire_server("entry", "show", "-selector", selector)
    assert "rel-examlops" not in entries and f"/ns/{NS}/sa/" in entries, entries
