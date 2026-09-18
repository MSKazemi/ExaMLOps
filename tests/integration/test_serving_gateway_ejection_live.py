"""The gateway must stop choosing a model server that has stopped answering (plan P5).

The node-loss drill (`tests/integration/test_serving_node_loss_kind_live.py`) measured what a lost
node costs: its pod keeps its place in the Service for **two minutes**, and everything routed there
hangs until the caller's timeout. Kubernetes gets there in its own time, and a client-side retry
cannot help — the retry is routed by the same Service and can land on the same dead address.

A proxy can do better, because it sees the answers. This drill holds the shipped Envoy
configuration to that: with two model-server endpoints and one of them **wedged** — accepting
connections and never answering, which is what a black-holed pod looks like — the gateway must
notice and route everything to the other one, in seconds rather than minutes.

It is run against the committed `gateway/envoy.yaml`, and against the same file with its health
checks stripped out, so the difference is measured rather than asserted.

Two things it also has to prove, because they are how such a change breaks a working platform:

- with a **single** endpoint and no healthy alternative, the gateway must keep sending requests to
  it rather than answering "no healthy upstream" (Envoy's panic threshold). A single-container
  Compose stack must not start failing closed because a model is slow to load;
- a healthy endpoint must never be ejected by the health check itself.

Opt-in, because it needs Docker::

    EXAMLOPS_GATEWAY_LIVE=1 .venv/bin/pytest \\
        tests/integration/test_serving_gateway_ejection_live.py -v -s
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENVOY_IMAGE = "envoyproxy/envoy:v1.39.1"
CONFIG = ROOT / "platform" / "infra" / "docker-compose" / "gateway" / "envoy.yaml"
MEASURED: dict[str, object] = {}

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("EXAMLOPS_GATEWAY_LIVE") != "1", reason="set EXAMLOPS_GATEWAY_LIVE=1"
    ),
]

# Short on purpose: a caller that waits 35 s (the gateway's route timeout) for every request sent to
# a black hole would make this drill take an hour. Real callers set a deadline too.
CLIENT_TIMEOUT = 3.0
REQUESTS = 60


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Upstream(BaseHTTPRequestHandler):
    """A model server stand-in that can be wedged: it accepts the connection and never answers.

    That is what a pod on a lost node looks like from a proxy — not a refusal, which is easy (the
    gateway already retries `connect-failure`), but silence, which costs the caller its whole
    deadline.
    """

    wedged = False
    served = 0  # inference requests only
    probed = 0  # health checks only — counting these as "served" made a premise vacuous once:
    # "the endpoint is back in rotation" was satisfied by the gateway's own `/ready` probes, so the
    # drill timed detection from a moment when no inference was going there at all.

    def _answer(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        probe = self.path.startswith("/ready")
        if type(self).wedged and not probe:
            time.sleep(120)  # the black hole: accepted, never answered
            return
        if type(self).wedged and probe:
            self.send_response(503)  # what the model server says when it cannot serve
            self.send_header("content-length", "0")
            self.end_headers()
            return
        if probe:
            type(self).probed += 1
        else:
            type(self).served += 1
        body = json.dumps({"served_by": type(self).__name__})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    do_GET = do_POST = _answer

    def log_message(self, *args) -> None:  # noqa: D401 - quiet
        return


class _UpstreamA(_Upstream):
    wedged = False
    served = 0
    probed = 0


class _UpstreamB(_Upstream):
    wedged = False
    served = 0
    probed = 0


def _serve(handler, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _wait(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)  # noqa: S310
            return
        except urllib.error.HTTPError:
            return
        except OSError:
            time.sleep(0.3)
    raise AssertionError(f"{url} never came up")


def _config_for(
    endpoints: list[int], *, authz_port: int, ports: dict[str, int], health: bool
) -> str:
    """The committed configuration, pointed at these endpoints. `health` strips the active health
    checks (and outlier detection) so the drill can measure what they are worth."""
    endpoint_yaml = "\n".join(
        f"              - endpoint:\n"
        f"                  address:\n"
        f"                    socket_address: {{address: 127.0.0.1, port_value: {port}}}"
        for port in endpoints
    )
    config = CONFIG.read_text().replace(
        "              - endpoint:\n"
        "                  address:\n"
        "                    socket_address: {address: ray-serving, port_value: 8001}",
        endpoint_yaml,
        1,
    )
    config = (
        config.replace(
            "address: ray-serving, port_value: 8081",
            f"address: 127.0.0.1, port_value: {ports['grpc']}",
        )
        .replace(
            "address: gateway-authz, port_value: 8090",
            f"address: 127.0.0.1, port_value: {authz_port}",
        )
        .replace("http://gateway-authz:8090", f"http://127.0.0.1:{authz_port}")
        .replace("port_value: 8080", f"port_value: {ports['gw']}")
        .replace("port_value: 9902", f"port_value: {ports['metrics']}")
        .replace("port_value: 9901", f"port_value: {ports['admin']}")
    )
    if not health:
        config = _strip_health_checks(config)
    return config


def _strip_health_checks(config: str) -> str:
    """Remove the `health_checks:` and `outlier_detection:` blocks — the configuration as it was
    before this drill existed."""
    out, skipping = [], False
    for line in config.split("\n"):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped.startswith(("health_checks:", "outlier_detection:")):
            skipping, skip_indent = True, indent
            continue
        if skipping:
            if stripped and indent <= skip_indent and not stripped.startswith("-"):
                skipping = False
            else:
                continue
        out.append(line)
    return "\n".join(out)


class Gateway:
    def __init__(self, tmp, name: str, config: str, ports: dict[str, int]) -> None:
        self.name = name
        self.ports = ports
        directory = tmp / name
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)
        (directory / "envoy.yaml").write_text(config)
        (directory / "envoy.yaml").chmod(0o644)
        # No `--rm`: when one of these does not come up, its logs are the only way to find out why.
        # `--disable-hot-restart`: several Envoys share one network namespace here, and the
        # hot-restart domain socket is per-namespace — the second instance dies with
        # "unable to bind domain socket with base_id=0". The chart passes the same flag.
        subprocess.run(
            ["docker", "run", "-d", "--name", name, "--network", "host",
             "-v", f"{directory}:/cfg:ro", ENVOY_IMAGE, "-c", "/cfg/envoy.yaml",
             "--log-level", "warn", "--disable-hot-restart"],
            check=True, capture_output=True,
        )  # fmt: skip

    def logs(self) -> str:
        out = subprocess.run(
            ["docker", "logs", "--tail", "40", self.name], capture_output=True, text=True
        )
        state = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}} exit={{.State.ExitCode}}", self.name],
            capture_output=True, text=True,
        )  # fmt: skip
        return f"[{state.stdout.strip()}]\n{(out.stdout + out.stderr)[-2000:]}"

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.ports['gw']}"

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


def _call(gw: Gateway, key: str, timeout: float = CLIENT_TIMEOUT) -> int | str:
    request = urllib.request.Request(  # noqa: S310
        gw.base + "/v2/models/jpcp/infer",
        data=b'{"inputs": []}',
        headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310
            return answer.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:  # noqa: BLE001 - a timeout is the measurement
        return type(exc).__name__


def _membership(gw: Gateway) -> tuple[float, float]:
    """(healthy, total) endpoints of the model-server cluster, as Prometheus scrapes them.

    This is what `ServingEndpointsUnhealthy` alerts on, so the drill reads the same numbers the
    alert would: an ejection nobody can see is an ejection nobody can act on.
    """
    body = (
        urllib.request.urlopen(  # noqa: S310
            f"http://127.0.0.1:{gw.ports['metrics']}/stats/prometheus", timeout=10
        )
        .read()
        .decode()
    )
    found = {}
    for name in ("healthy", "total"):
        m = re.search(
            rf'^envoy_cluster_membership_{name}{{envoy_cluster_name="ray_serving"}} ([0-9.]+)$',
            body,
            re.M,
        )
        found[name] = float(m.group(1)) if m else -1.0
    return found["healthy"], found["total"]


def _sweep(gw: Gateway, key: str, count: int = REQUESTS) -> dict[str, int]:
    outcomes: dict[str, int] = {}
    for _ in range(count):
        outcome = str(_call(gw, key))
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ejection")
    db = tmp / "platform.db"
    env = {**os.environ, "PLATFORM_DB": str(db)}
    env.pop("EXAMLOPS_DB_BACKEND", None)
    os.environ["PLATFORM_DB"] = str(db)
    os.environ.pop("EXAMLOPS_MULTITENANCY", None)
    sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))
    from examlops import gateway as vkeys

    key = vkeys.issue_virtual_key("acme", "research", None, None, "test")

    ports = {
        "a": _port(), "b": _port(), "grpc": _port(), "authz": _port(),
        "gw": _port(), "metrics": _port(), "admin": _port(),
        "gw2": _port(), "metrics2": _port(), "admin2": _port(),
        "gw3": _port(), "metrics3": _port(), "admin3": _port(),
    }  # fmt: skip
    servers = [_serve(_UpstreamA, ports["a"]), _serve(_UpstreamB, ports["b"])]
    authz = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "--factory", "examlops.serving_gateway:create_app",
         "--host", "127.0.0.1", "--port", str(ports["authz"]), "--log-level", "warning"],
        env={**env, "PYTHONPATH": str(ROOT / "platform" / "cli" / "src")},
    )  # fmt: skip
    gateways: list[Gateway] = []
    try:
        _wait(f"http://127.0.0.1:{ports['authz']}/healthz")
        shipped = Gateway(
            tmp, f"exa-eject-shipped-{ports['gw']}",
            _config_for([ports["a"], ports["b"]], authz_port=ports["authz"],
                        ports={"gw": ports["gw"], "metrics": ports["metrics"],
                               "admin": ports["admin"], "grpc": ports["grpc"]}, health=True),
            {"gw": ports["gw"], "metrics": ports["metrics"], "admin": ports["admin"]},
        )  # fmt: skip
        gateways.append(shipped)
        bare = Gateway(
            tmp, f"exa-eject-bare-{ports['gw2']}",
            _config_for([ports["a"], ports["b"]], authz_port=ports["authz"],
                        ports={"gw": ports["gw2"], "metrics": ports["metrics2"],
                               "admin": ports["admin2"], "grpc": ports["grpc"]}, health=False),
            {"gw": ports["gw2"], "metrics": ports["metrics2"], "admin": ports["admin2"]},
        )  # fmt: skip
        gateways.append(bare)
        # A third one with a *single* endpoint, to prove the change cannot take a one-container
        # deployment out of service when its only model server is unhealthy.
        solo = Gateway(
            tmp, f"exa-eject-solo-{ports['gw3']}",
            _config_for([ports["b"]], authz_port=ports["authz"],
                        ports={"gw": ports["gw3"], "metrics": ports["metrics3"],
                               "admin": ports["admin3"], "grpc": ports["grpc"]}, health=True),
            {"gw": ports["gw3"], "metrics": ports["metrics3"], "admin": ports["admin3"]},
        )  # fmt: skip
        gateways.append(solo)
        for gw in gateways:
            try:
                _wait(f"{gw.base}/v2/health/live")
            except AssertionError as exc:
                raise AssertionError(f"{gw.name} did not serve: {exc}\n{gw.logs()}") from exc
        yield {"shipped": shipped, "bare": bare, "solo": solo, "key": key, "ports": ports}
    finally:
        if MEASURED:
            print("\ngateway ejection drill:", json.dumps(MEASURED, indent=1))
        for gw in gateways:
            gw.stop()
        authz.terminate()
        authz.wait(10)
        for server in servers:
            server.shutdown()


def _wedge(wedged: bool) -> None:
    _UpstreamB.wedged = wedged


# ── the drill ─────────────────────────────────────────────────────────────────


def test_1_both_endpoints_healthy_everything_is_served(stack):
    _wedge(False)
    outcomes = _sweep(stack["shipped"], stack["key"])
    MEASURED["both_healthy"] = outcomes
    assert outcomes == {"200": REQUESTS}, outcomes
    assert _UpstreamA.served and _UpstreamB.served, "the load never reached both endpoints"
    # And the shipped configuration really is probing them, which is what the rest of this rests on.
    assert _until(lambda: _UpstreamA.probed and _UpstreamB.probed, 30), (
        "no health check reached the endpoints"
    )
    MEASURED["health_checks_seen"] = {"a": _UpstreamA.probed, "b": _UpstreamB.probed}


def test_2_without_health_checks_a_black_hole_keeps_taking_its_share(stack):
    """The configuration as it was: Envoy has no reason to stop choosing an endpoint that accepts
    connections and never answers, so a caller keeps paying its deadline for half the requests."""
    _wedge(True)
    time.sleep(15)  # as long as the shipped gateway is given below, so the comparison is fair
    outcomes = _sweep(stack["bare"], stack["key"], count=20)
    MEASURED["bare_with_one_black_hole"] = outcomes
    failures = sum(count for outcome, count in outcomes.items() if outcome != "200")
    MEASURED["bare_failures"] = failures
    assert failures > 0, (
        "a wedged endpoint cost the gateway nothing without health checks, so this drill cannot "
        f"show what they are worth: {outcomes}"
    )


def test_3_the_shipped_gateway_stops_choosing_the_black_hole(stack):
    """What the health check buys: the gateway notices within seconds and serves everything from the
    endpoint that answers — no operator, no Kubernetes, no client retry involved.

    The endpoint is brought back and confirmed healthy first. Without that, the previous test has
    already wedged it, this gateway has already ejected it, and the "detection time" measured here
    is 0.2 s of nothing happening — which is what the first version of this test reported.
    """
    _wedge(False)
    assert _until(lambda: _sweep(stack["shipped"], stack["key"], count=6) == {"200": 6}, 60), (
        "the endpoint never came back healthy, so detection cannot be timed from here"
    )
    before = _UpstreamB.served
    assert _until(
        lambda: (_sweep(stack["shipped"], stack["key"], count=12), _UpstreamB.served > before)[1],
        60,
    ), "the recovered endpoint is not in rotation yet"
    _wedge(True)
    began = time.monotonic()
    deadline = began + 60
    while time.monotonic() < deadline:
        if _sweep(stack["shipped"], stack["key"], count=8) == {"200": 8}:
            MEASURED["seconds_until_the_black_hole_was_ejected"] = round(
                time.monotonic() - began, 1
            )
            break
        time.sleep(2)
    else:
        pytest.fail("the gateway never stopped routing to the wedged endpoint")
    outcomes = _sweep(stack["shipped"], stack["key"])
    MEASURED["shipped_after_ejection"] = outcomes
    assert outcomes == {"200": REQUESTS}, outcomes
    # And an operator can see it: this is the metric `ServingEndpointsUnhealthy` watches.
    healthy, total = _membership(stack["shipped"])
    MEASURED["membership_while_ejected"] = {"healthy": healthy, "total": total}
    assert total == 2 and healthy == 1, (healthy, total)


def test_4_the_endpoint_comes_back_by_itself(stack):
    """Ejection is not a one-way door: the health check has to put it back, without a restart."""
    _wedge(False)
    began = time.monotonic()
    before = _UpstreamB.served
    assert _until(
        lambda: (_sweep(stack["shipped"], stack["key"], count=10), _UpstreamB.served > before)[1],
        90,
    ), "the recovered endpoint never got traffic again"
    MEASURED["seconds_until_the_endpoint_was_used_again"] = round(time.monotonic() - began, 1)
    healthy, total = _membership(stack["shipped"])
    MEASURED["membership_after_recovery"] = {"healthy": healthy, "total": total}
    assert (healthy, total) == (2, 2), (healthy, total)  # the alert clears by itself too
    outcomes = _sweep(stack["shipped"], stack["key"], count=40)
    MEASURED["after_recovery"] = outcomes
    assert outcomes == {"200": 40}, outcomes


def test_5_a_lone_unhealthy_endpoint_is_still_used(stack):
    """The safety property. Envoy's panic threshold exists for exactly this: when no endpoint is
    healthy, ejecting them all would turn a degraded model server into a dead one. A single-container
    Compose stack whose model is still loading must keep getting its requests through, not
    `no healthy upstream`.

    "Still used" is the claim, not "still answered": the endpoint is wedged, so the caller's own
    deadline is what ends the request. What matters is that the gateway's answer is not a refusal it
    invented.
    """
    _wedge(True)
    time.sleep(20)  # long enough for the health check to have marked it down
    outcomes = _sweep(stack["solo"], stack["key"], count=8)
    MEASURED["lone_unhealthy_endpoint"] = outcomes
    assert "503" not in outcomes, (
        f"the gateway refused on its own behalf with no alternative to offer: {outcomes}"
    )
    _wedge(False)


def _until(fn, timeout: float, interval: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False
