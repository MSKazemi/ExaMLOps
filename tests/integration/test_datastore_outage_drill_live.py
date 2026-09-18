"""Chaos drill: the platform datastore (Postgres) dies under load and comes back (plan P5).

A throwaway Docker network with Postgres, a Prefect stand-in, the control plane and the serving
gateway's authorization service, both on Postgres and both from an image built from this tree. The
drill kills Postgres hard (SIGKILL), holds the outage, starts it again, and holds each component to
what the docs promise:

- **the control plane says it is not ready, and fast.** ``/readyz`` answers 503 within seconds and
  never hangs past a probe's timeout, so an orchestrator takes the replica out of rotation;
- **writes fail cleanly.** A retrain submitted during the outage is refused with a 5xx that says the
  store is unavailable, and nothing half-written survives: after recovery it is simply absent;
- **serving keeps working where it can** (static stability, ADR 0123). A virtual key the gateway
  verified before the outage is still allowed from its cache; a key it has never seen is refused
  with 503, never waved through; no credential is still 401;
- **everything recovers on its own.** No container is restarted: the control plane is ready again,
  accepts retrains that run to completion, a retry of a pre-outage retrain is the same command, the
  never-seen key verifies, and the audit hash chain still verifies end to end.

Opt-in and slow (it needs Docker and an image build)::

    docker build -f platform/services/control_plane/Dockerfile \\
        -t exa-chaos/examlops-control-plane:tree .
    EXAMLOPS_CHAOS_LIVE=1 .venv/bin/pytest tests/integration/test_datastore_outage_drill_live.py -v -s

``-s`` prints the measured timings (time to unready, recovery time).
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
import uuid

import httpx
import pytest

from tests.integration.test_control_plane_failover_kind_live import PREFECT_STUB

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("EXAMLOPS_CHAOS_LIVE") != "1", reason="set EXAMLOPS_CHAOS_LIVE=1"),
]

IMAGE = "exa-chaos/examlops-control-plane:tree"
POSTGRES = "postgres:17-alpine"
PYTHON = "python:3.12-slim"
OUTAGE_SECONDS = 20
# /readyz must answer within the probe timeout the chart sets, even with the store gone.
PROBE_TIMEOUT = 3.0
TIMINGS: dict[str, float] = {}


def _run(*args: str, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess:
    out = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    if check and out.returncode != 0:
        raise AssertionError(f"{' '.join(args[:6])}…: {out.stderr[-2000:]}")
    return out


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _until(fn, timeout: float, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = fn()
        if found:
            return found
        time.sleep(interval)
    return None


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    if _run("docker", "image", "inspect", IMAGE, check=False).returncode:
        pytest.skip(f"build {IMAGE} first (see the module docstring)")
    tag = uuid.uuid4().hex[:8]
    net = f"exa-chaos-{tag}"
    names = {k: f"exa-chaos-{k}-{tag}" for k in ("pg", "prefect", "cp", "authz")}
    tmp = tmp_path_factory.mktemp("chaos")
    tmp.chmod(0o755)
    password = secrets.token_hex(16)
    token = secrets.token_hex(24)
    dsn = f"postgresql://examlops:{password}@postgres:5432/examlops"
    cp_port, authz_port = _port(), _port()
    try:
        _run("docker", "network", "create", net)
        # No --rm: a killed container keeps its filesystem, so `docker start` brings the data back.
        _run(
            "docker", "run", "-d", "--name", names["pg"], "--network", net,
            "--network-alias", "postgres", "-e", "POSTGRES_USER=examlops",
            "-e", f"POSTGRES_PASSWORD={password}", "-e", "POSTGRES_DB=examlops", POSTGRES,
        )  # fmt: skip
        (tmp / "prefect.py").write_text(PREFECT_STUB)
        (tmp / "prefect.py").chmod(0o644)
        _run(
            "docker", "run", "-d", "--rm", "--name", names["prefect"], "--network", net,
            "--network-alias", "prefect", "-v", f"{tmp}:/w:ro", PYTHON, "python", "/w/prefect.py",
        )  # fmt: skip
        assert _until(
            lambda: _run("docker", "exec", names["pg"], "psql", "-U", "examlops", "-d", "examlops",
                         "-qc", "select 1", check=False).returncode == 0,
            timeout=60,
        ), "postgres never accepted connections"  # fmt: skip
        common = [
            "-e", "EXAMLOPS_DB_BACKEND=postgres", "-e", f"EXAMLOPS_POSTGRES_DSN={dsn}",
            "-e", "OTEL_SDK_DISABLED=true", "-e", "EXAMLOPS_ACTOR=chaos-drill",
        ]  # fmt: skip
        _run(
            "docker", "run", "-d", "--rm", "--name", names["cp"], "--network", net,
            "-p", f"127.0.0.1:{cp_port}:8002", *common,
            "-e", f"CONTROL_PLANE_TOKEN={token}",
            "-e", "PREFECT_API_URL=http://prefect:4200/api",
            "-e", "MODELZOO_POLL_SECONDS=0", "-e", "CONTROL_PLANE_RECONCILE_SECONDS=1",
            "-e", "CONTROL_PLANE_STARTUP_RECHECK_SECONDS=2",
            IMAGE,
        )  # fmt: skip
        _run(
            "docker", "run", "-d", "--rm", "--name", names["authz"], "--network", net,
            "-p", f"127.0.0.1:{authz_port}:8090", *common,
            "-e", "EXAMLOPS_GATEWAY_KEY_CACHE_SECONDS=300", "-e", "EXAMLOPS_GATEWAY_TENANT_RPM=0",
            IMAGE, "uvicorn", "--factory", "examlops.serving_gateway:create_app",
            "--host", "0.0.0.0", "--port", "8090", "--log-level", "warning",
        )  # fmt: skip
        cp = f"http://127.0.0.1:{cp_port}"
        authz = f"http://127.0.0.1:{authz_port}"
        assert _until(lambda: _get(cp + "/readyz")[0] == 200, timeout=120), _logs(names["cp"])
        assert _until(lambda: _get(authz + "/healthz")[0] == 200, timeout=60), _logs(names["authz"])
        keys = {}
        for label in ("seen", "unseen"):
            keys[label] = _run(
                "docker", "exec", names["authz"], "python", "-c",
                "from examlops import gateway; "
                f"print(gateway.issue_virtual_key('chaos', 'drill', None, None, '{label}'))",
            ).stdout.strip().splitlines()[-1]  # fmt: skip
        yield {"cp": cp, "authz": authz, "token": token, "keys": keys, "names": names}
    finally:
        for name in names.values():
            _run("docker", "rm", "-f", name, check=False)
        _run("docker", "network", "rm", net, check=False)
        if TIMINGS:
            print("\nchaos drill timings:", json.dumps(TIMINGS, indent=1))


def _get(url: str, **kw) -> tuple[int, str]:
    try:
        r = httpx.get(url, timeout=kw.pop("timeout", PROBE_TIMEOUT), **kw)
        return r.status_code, r.text
    except httpx.TimeoutException:
        return -1, "timeout"
    except httpx.TransportError as exc:
        return 0, str(exc)


def _logs(name: str) -> str:
    return _run("docker", "logs", "--tail", "60", name, check=False).stderr[-4000:]


def _check(stack, key: str | None) -> int:
    headers = {"authorization": f"Bearer {key}"} if key else {}
    return _get(stack["authz"] + "/check/v2/models/jpcp/infer", headers=headers)[0]


def _retrain(stack, key: str, model: str = "JPCP", dataset: str = "PM100Dataset"):
    try:
        r = httpx.post(
            stack["cp"] + "/v1/retrain",
            json={"model_name": model, "dataset_name": dataset},
            headers={"Authorization": f"Bearer {stack['token']}", "Idempotency-Key": key},
            timeout=15,
        )
    except httpx.TimeoutException:
        return -1, {"error": "timeout"}
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"raw": r.text[:300]}


def _command(stack, command_id: str) -> dict:
    r = httpx.get(
        f"{stack['cp']}/v1/commands/{command_id}",
        headers={"Authorization": f"Bearer {stack['token']}"},
        timeout=10,
    )
    return r.json() if r.status_code == 200 else {"state": f"http {r.status_code}"}


def _settled(stack, command_id: str, timeout: float = 90) -> dict:
    final: dict = {}

    def done() -> bool:
        final.update(_command(stack, command_id))
        return final.get("state") in ("succeeded", "failed", "dead", "cancelled")

    _until(done, timeout=timeout, interval=1)
    return final


def _started_at(name: str) -> str:
    return _run("docker", "inspect", "-f", "{{.State.StartedAt}}", name).stdout.strip()


# ── the drill, in order ───────────────────────────────────────────────────────


def test_1_before_the_outage_everything_works(stack):
    assert _get(stack["cp"] + "/readyz")[0] == 200
    assert _check(stack, stack["keys"]["seen"]) == 200  # the gateway has now seen this key
    status, body = _retrain(stack, "before-outage")
    assert status == 202, body
    stack["before"] = body["command_id"]
    assert _settled(stack, body["command_id"])["state"] == "succeeded"
    stack["started"] = {n: _started_at(stack["names"][n]) for n in ("cp", "authz")}


def test_2_during_the_outage_the_platform_degrades_as_documented(stack):
    _run("docker", "kill", stack["names"]["pg"])
    killed = time.monotonic()
    slowest = 0.0

    def unready() -> bool:
        nonlocal slowest
        began = time.monotonic()
        code, _ = _get(stack["cp"] + "/readyz")
        slowest = max(slowest, time.monotonic() - began)
        return code == 503

    assert _until(unready, timeout=30), "the control plane kept reporting ready without its store"
    TIMINGS["seconds_to_unready"] = round(time.monotonic() - killed, 2)
    TIMINGS["slowest_readyz_during_outage"] = round(slowest, 2)
    assert slowest < PROBE_TIMEOUT, f"/readyz took {slowest:.1f} s: an orchestrator probe times out"

    # A write fails cleanly: a 5xx that says the store is unavailable, not a crash or a hang.
    status, body = _retrain(stack, "during-outage")
    TIMINGS["retrain_status_during_outage"] = status
    assert status in (503,), (status, body)

    # Serving keeps working where it can (static stability).
    assert _check(stack, stack["keys"]["seen"]) == 200, "a verified key stopped working"
    assert _check(stack, stack["keys"]["unseen"]) == 503, "a never-verified key was not refused"
    assert _check(stack, None) == 401

    remaining = OUTAGE_SECONDS - (time.monotonic() - killed)
    if remaining > 0:
        time.sleep(remaining)
    assert _check(stack, stack["keys"]["seen"]) == 200  # still, deep into the outage


def test_3_after_the_outage_everything_recovers_without_a_restart(stack):
    _run("docker", "start", stack["names"]["pg"])
    started = time.monotonic()
    # Postgres replays its write-ahead log after a SIGKILL before it accepts connections. Measured
    # apart from the platform's own recovery, which is what this drill is about.
    assert _until(
        lambda: _run("docker", "exec", stack["names"]["pg"], "psql", "-U", "examlops",
                     "-d", "examlops", "-qc", "select 1", check=False).returncode == 0,
        timeout=90,
    ), "postgres did not come back"  # fmt: skip
    accepting = time.monotonic()
    TIMINGS["seconds_postgres_took_to_accept"] = round(accepting - started, 2)
    assert _until(lambda: _get(stack["cp"] + "/readyz")[0] == 200, timeout=90), _logs(
        stack["names"]["cp"]
    )
    TIMINGS["seconds_control_plane_took_after_that"] = round(time.monotonic() - accepting, 2)
    for name in ("cp", "authz"):
        assert _started_at(stack["names"][name]) == stack["started"][name], f"{name} restarted"

    status, body = _retrain(stack, "after-outage", model="MACK", dataset="FDataDataset")
    assert status == 202, body
    assert _settled(stack, body["command_id"])["state"] == "succeeded"
    # A retry of the pre-outage submission is the same command, not a second run.
    status, body = _retrain(stack, "before-outage")
    assert status in (200, 202), body
    assert body["command_id"] == stack["before"]
    # The refused submission left nothing behind.
    status, body = _retrain(stack, "during-outage")
    assert status == 202 and body["command_id"] != stack["before"], body

    assert _until(lambda: _check(stack, stack["keys"]["unseen"]) == 200, timeout=30)
    chain = _run(
        "docker", "exec", stack["names"]["cp"], "python", "-c",
        "import json; from examlops.data.audit import verify_audit_chain; "
        "print(json.dumps(verify_audit_chain()))",
    ).stdout.strip().splitlines()[-1]  # fmt: skip
    verdict = json.loads(chain)
    assert verdict.get("ok", verdict.get("valid")) is True, verdict
