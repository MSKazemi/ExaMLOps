"""The Compose identity overlay issues each service its identity by container label (ADR 0125).

Brings up the shipped `platform/infra/docker-compose/identity/compose.yml` on its own, in a
throwaway Compose project: the PKI and registration one-shots, the SPIRE server, the read-only
Docker API proxy, the SPIRE agent with the Docker workload attestor, and the spiffe-helpers. Then:

- the autopilot's helper keeps a JWT-SVID that verifies, with `examlops.workload_identity`, as
  `spiffe://examlops.internal/autopilot` against the JWT bundle the control plane's own helper
  keeps, and a platform client sends it through `CONTROL_PLANE_TOKEN_FILE`;
- the service's non-root user can read it from the read-only volume;
- a container without an identity label gets nothing;
- the one-shots are idempotent (a second `up` registers no duplicates);
- the identities survive an agent restart and a server restart (disk-backed keys).

Opt-in, because it needs Docker and runs an agent in the host PID namespace::

    EXAMLOPS_SPIRE_LIVE=1 .venv/bin/pytest tests/integration/test_spire_compose_attestation_live.py -v
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "platform" / "cli" / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("EXAMLOPS_SPIRE_LIVE") != "1", reason="set EXAMLOPS_SPIRE_LIVE=1"),
]

DOMAIN = "examlops.internal"
IDENTITY = ROOT / "platform" / "infra" / "docker-compose" / "identity"
HELPER = "ghcr.io/spiffe/spiffe-helper:0.11.0"
READER = "alpine:3.22"
SERVER_SOCKET = "/run/spire/server/private/api.sock"


def _run(*args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), check=check, capture_output=True, text=True, env=env)


def _until(fn, timeout: float = 90.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = fn()
        if found:
            return found
        time.sleep(1)
    return None


class Stack:
    def __init__(self) -> None:
        tag = uuid.uuid4().hex[:8]
        self.project = f"exa-spid-{tag}"
        self.env = {**os.environ, "EXAMLOPS_IMAGE_PREFIX": self.project}

    def compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return _run(
            "docker", "compose", "-f", str(IDENTITY / "compose.yml"), "-p", self.project,
            "--profile", "events", *args, check=check, env=self.env,
        )  # fmt: skip

    def volume(self, name: str) -> str:
        return f"{self.project}_{name}"

    def read(self, volume: str, file: str, user: str = "0") -> str:
        out = _run(
            "docker", "run", "--rm", "--network", "none", "--user", user,
            "-v", f"{self.volume(volume)}:/s:ro", READER, "cat", f"/s/{file}", check=False,
        )  # fmt: skip
        return out.stdout if out.returncode == 0 else ""

    def entries(self) -> str:
        return self.compose(
            "exec", "-T", "spire-server", "/opt/spire/bin/spire-server", "entry", "show",
            "-socketPath", SERVER_SOCKET,
        ).stdout  # fmt: skip


@pytest.fixture(scope="module")
def stack():
    s = Stack()
    try:
        up = s.compose("up", "-d", "--build", "--wait", "--wait-timeout", "180", check=False)
        assert up.returncode == 0, up.stderr[-3000:] + s.compose("logs", check=False).stdout[-3000:]
        yield s
    finally:
        s.compose("down", "-v", "--remove-orphans", check=False)
        _run("docker", "image", "rm", "-f", f"{s.project}-spire-init:latest", check=False)


def _svid(stack: Stack) -> str:
    token = _until(lambda: stack.read("svid_autopilot", "control-plane.jwt").strip())
    assert token, stack.compose("logs", "spiffe-helper-autopilot", check=False).stdout[-2000:]
    return token


@pytest.fixture()
def verifier(stack, tmp_path, monkeypatch):
    """Point the verifier at the bundle the control plane's helper keeps."""
    bundle_text = _until(lambda: stack.read("svid_control_plane", "jwt-bundle.json"))
    assert bundle_text, stack.compose("logs", "spiffe-helper-control-plane", check=False).stdout
    bundle = tmp_path / "jwt-bundle.json"
    bundle.write_text(bundle_text)
    monkeypatch.setenv("EXAMLOPS_SPIFFE_TRUST_DOMAIN", DOMAIN)
    monkeypatch.setenv("EXAMLOPS_SPIFFE_BUNDLE", str(bundle))
    from examlops import workload_identity

    return workload_identity


def test_the_labelled_helper_keeps_its_services_identity(stack, verifier, tmp_path, monkeypatch):
    from examlops import service_auth

    token_file = tmp_path / "control-plane.jwt"
    token_file.write_text(_svid(stack))
    monkeypatch.setenv("CONTROL_PLANE_TOKEN_FILE", str(token_file))
    sent = service_auth.control_plane_bearer("static")  # what a platform client would send
    workload = verifier.verify(sent, "control-plane")
    assert workload.spiffe_id == f"spiffe://{DOMAIN}/autopilot"
    assert workload.expires_at - time.time() <= 300 + 30  # SPIFFE_JWT_SVID_TTL


def test_the_services_own_user_can_read_it(stack):
    _svid(stack)
    assert stack.read("svid_autopilot", "control-plane.jwt", user="10001:10001").strip()


def test_a_container_without_the_label_gets_nothing(stack):
    name = f"{stack.project}-stranger"
    out = Path(tempfile.mkdtemp(prefix="exa-spid-stranger-"))
    out.chmod(0o777)
    _run(
        "docker", "run", "-d", "--rm", "--name", name, "--network", "none",
        "--label", "examlops.spiffe=nobody",
        "-v", f"{stack.volume('spire_agent_socket')}:/run/spire/sockets:ro",
        "-v", f"{IDENTITY / 'helper-caller.conf'}:/conf/helper.conf:ro",
        "-v", f"{out}:/run/spire/svid", HELPER, "-config", "/conf/helper.conf",
    )  # fmt: skip
    try:
        _svid(stack)  # the labelled helper, same config, has its identity
        time.sleep(15)  # far longer than the labelled helper needed
        assert _run("docker", "inspect", name, check=False).returncode == 0  # still asking
        assert not (out / "control-plane.jwt").exists()
    finally:
        _run("docker", "rm", "-f", name, check=False)


def test_the_setup_is_idempotent(stack):
    before = stack.entries()
    again = stack.compose("up", "-d", "--wait", "--wait-timeout", "120", check=False)
    assert again.returncode == 0, again.stderr[-2000:]
    after = stack.entries()
    assert "Found 8 entries" in after, after  # the node alias and seven workloads, once each
    assert before.count("Entry ID") == after.count("Entry ID")


def test_identities_survive_agent_and_server_restarts(stack, verifier):
    old = _svid(stack)
    stack.compose("restart", "spire-server", "spire-agent")
    stack.compose("up", "-d", "--wait", "--wait-timeout", "120")

    def renewed():
        token = stack.read("svid_autopilot", "control-plane.jwt").strip()
        return token if token and token != old else ""

    # The helper rewrites the SVID at half its lifetime; after the restarts it must still manage.
    token = _until(renewed, timeout=240)
    assert token, stack.compose("logs", "spiffe-helper-autopilot", check=False).stdout[-2000:]
    # Disk-backed keys: the bundle fetched before the restart still verifies the new SVID.
    assert verifier.verify(token, "control-plane").spiffe_id == f"spiffe://{DOMAIN}/autopilot"


def test_the_bundle_set_names_this_trust_domain(stack):
    """spiffe-helper writes {trust domain: base64(JWKS)}; the verifier reads its own entry."""
    document = json.loads(_until(lambda: stack.read("svid_control_plane", "jwt-bundle.json")))
    assert list(document) == [DOMAIN], document
    keys = json.loads(base64.b64decode(document[DOMAIN]))["keys"]
    assert keys and all(k["kty"] in ("EC", "RSA") and k.get("kid") for k in keys), keys
