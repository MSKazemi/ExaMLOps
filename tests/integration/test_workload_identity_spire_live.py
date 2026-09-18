"""A real SPIRE issues a JWT-SVID, and the control plane accepts it (ADR 0125 phase 1, Docker).

Starts a SPIRE server and agent (1.15.3) on a private Docker network: the agent attests with a
join token, a workload is registered by Unix uid, and the agent's Workload API hands that
workload a JWT-SVID for the control plane's audience. The server's own bundle
(`spire-server bundle show -format spiffe`) is the trust bundle. `examlops.workload_identity`
must accept the SVID, and the control plane must give it the mapped workload's scopes; an SVID for
another audience must be refused. Nothing here is a hand-made token.

Opt-in, because it needs Docker::

    EXAMLOPS_SPIRE_LIVE=1 .venv/bin/pytest tests/integration/test_workload_identity_spire_live.py -v
"""

from __future__ import annotations

import importlib
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
for p in (
    str(ROOT / "platform" / "cli" / "src"),
    str(ROOT / "platform" / "services" / "control_plane"),
):
    if p not in sys.path:
        sys.path.insert(0, p)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("EXAMLOPS_SPIRE_LIVE") != "1", reason="set EXAMLOPS_SPIRE_LIVE=1"),
]

VERSION = "1.15.3"
DOMAIN = "examlops.internal"
SOCKET = "/tmp/spire-agent/public/api.sock"

SERVER_CONF = f"""
server {{
  bind_address = "0.0.0.0"
  bind_port = "8081"
  trust_domain = "{DOMAIN}"
  data_dir = "/tmp/spire-data"
  log_level = "WARN"
  default_jwt_svid_ttl = "5m"
}}
plugins {{
  DataStore "sql" {{ plugin_data {{ database_type = "sqlite3"
    connection_string = "/tmp/spire-data/datastore.sqlite3" }} }}
  KeyManager "memory" {{ plugin_data {{}} }}
  NodeAttestor "join_token" {{ plugin_data {{}} }}
}}
"""

AGENT_CONF = f"""
agent {{
  data_dir = "/tmp/spire-agent"
  log_level = "WARN"
  server_address = "spire-server"
  server_port = "8081"
  socket_path = "{SOCKET}"
  trust_domain = "{DOMAIN}"
  insecure_bootstrap = true
}}
plugins {{
  NodeAttestor "join_token" {{ plugin_data {{}} }}
  KeyManager "memory" {{ plugin_data {{}} }}
  WorkloadAttestor "unix" {{ plugin_data {{}} }}
}}
"""


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def _until(fn, timeout: float = 60.0):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        last = fn()
        if last.returncode == 0:
            return last
        time.sleep(1)
    raise AssertionError(f"timed out: {last.stderr if last else ''}")


@pytest.fixture(scope="module")
def spire():
    tag = uuid.uuid4().hex[:8]
    net, server, agent = f"exa-spire-{tag}", f"exa-spire-s-{tag}", f"exa-spire-a-{tag}"
    conf = Path(tempfile.mkdtemp(prefix="exa-spire-"))
    conf.chmod(0o755)
    (conf / "server.conf").write_text(SERVER_CONF)
    (conf / "agent.conf").write_text(AGENT_CONF)
    for f in conf.iterdir():
        f.chmod(0o644)
    server_bin = "/opt/spire/bin/spire-server"
    _docker("network", "create", net)
    try:
        _docker(
            "run", "-d", "--rm", "--name", server, "--network", net,
            "--network-alias", "spire-server", "-v", f"{conf}:/conf:ro",
            f"ghcr.io/spiffe/spire-server:{VERSION}", "-config", "/conf/server.conf",
        )  # fmt: skip
        _until(lambda: _docker("exec", server, server_bin, "healthcheck", check=False))
        token = _docker(
            "exec", server, server_bin, "token", "generate",
            "-spiffeID", f"spiffe://{DOMAIN}/agent",
        ).stdout.split()[-1]  # fmt: skip
        _docker(
            "run", "-d", "--rm", "--name", agent, "--network", net, "-v", f"{conf}:/conf:ro",
            f"ghcr.io/spiffe/spire-agent:{VERSION}", "-config", "/conf/agent.conf",
            "-joinToken", token,
        )  # fmt: skip
        # The image is distroless (no `id`); `docker exec` runs as the image's configured user.
        user = _docker("inspect", agent, "--format", "{{.Config.User}}").stdout.strip()
        uid = (user.split(":")[0] or "0") if user else "0"
        _docker(
            "exec", server, server_bin, "entry", "create",
            "-parentID", f"spiffe://{DOMAIN}/agent",
            "-spiffeID", f"spiffe://{DOMAIN}/autopilot",
            "-selector", f"unix:uid:{uid}",
        )  # fmt: skip
        bundle = _docker("exec", server, server_bin, "bundle", "show", "-format", "spiffe").stdout
        yield {"agent": agent, "bundle": bundle}
    finally:
        _docker("rm", "-f", server, agent, check=False)
        _docker("network", "rm", net, check=False)


def _fetch_svid(spire, audience: str) -> str:
    out = _until(
        lambda: _docker(
            "exec", spire["agent"], "/opt/spire/bin/spire-agent", "api", "fetch", "jwt",
            "-audience", audience, "-socketPath", SOCKET, "-output", "json",
            check=False,
        ),
        timeout=90,
    )  # fmt: skip
    document = json.loads(out.stdout)
    svids = document[0]["svids"] if isinstance(document, list) else document["svids"]
    return svids[0]["svid"]


@pytest.fixture()
def configured(spire, tmp_path, monkeypatch):
    bundle = tmp_path / "bundle.json"
    bundle.write_text(spire["bundle"])
    monkeypatch.setenv("EXAMLOPS_SPIFFE_TRUST_DOMAIN", DOMAIN)
    monkeypatch.setenv("EXAMLOPS_SPIFFE_BUNDLE", str(bundle))
    return spire


def test_the_platform_verifies_an_svid_spire_issued(configured):
    from examlops import workload_identity

    svid = _fetch_svid(configured, "control-plane")
    workload = workload_identity.verify(svid, "control-plane")
    assert workload.spiffe_id == f"spiffe://{DOMAIN}/autopilot"
    assert workload.expires_at - time.time() <= 5 * 60 + 30  # short-lived, as configured
    with pytest.raises(workload_identity.WorkloadIdentityError):
        workload_identity.verify(_fetch_svid(configured, "dashboard"), "control-plane")


def test_the_control_plane_acts_on_it_with_the_mapped_scopes(configured, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv(
        "CONTROL_PLANE_WORKLOAD_IDENTITIES_JSON",
        json.dumps(
            {
                f"spiffe://{DOMAIN}/autopilot": {
                    "principal": "autopilot",
                    "tenant": "default",
                    "scopes": ["read", "retrain"],
                }
            }
        ),
    )
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_COMMAND_WORKERS", "0")
    monkeypatch.delenv("CONTROL_PLANE_TOKEN", raising=False)
    import app as cp_app

    importlib.reload(cp_app)
    client = TestClient(cp_app.app)
    svid = _fetch_svid(configured, "control-plane")
    answer = client.get("/v1/commands", headers={"Authorization": f"Bearer {svid}"})
    assert answer.status_code == 200, answer.text
    other = _fetch_svid(configured, "dashboard")
    refused = client.get("/v1/commands", headers={"Authorization": f"Bearer {other}"})
    assert refused.status_code == 403
