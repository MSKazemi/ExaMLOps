"""Every hop to the model server is mutual TLS with SPIRE identities (ADR 0125 phases 2 and 3).

Live, with the shipped files: SPIRE from `identity/compose.yml`, the gateway Envoy on
`gateway/envoy-mtls.yaml`, the serving-side Envoy on `identity/serving-mtls-envoy.yaml` in the
network namespace of a stand-in model server that listens on loopback only, and the real
authorization service. Both Envoys get their certificates from the SPIRE agent over SDS, by their
container labels (`examlops.spiffe=gateway`, `examlops.spiffe=ray-serving`). Checked:

- a request with a virtual key goes gateway ─mTLS─▶ sidecar ─loopback─▶ model server, and the model
  server is told the verified caller (`x-forwarded-client-cert` naming …/gateway);
- the gateway's own checks still apply (anonymous → 401);
- at the model server's TLS port, a client with no certificate is refused, and so is a client
  holding a valid X.509-SVID of the trust domain that is no serving caller (…/autopilot); the same
  client with the gateway's or the dashboard's SVID gets through, so the identity is what decides;
- a caller's egress sidecar (`identity/serving-egress-envoy.yaml`, in the caller's network
  namespace, labelled with the caller's identity) carries plain HTTP from the caller's loopback to
  the model server under that identity: the dashboard may infer and reload, the SeanerBUS bridge
  may infer but every spelling of an admin route is refused, and a sidecar with an identity that is
  no caller gets nothing through.

Opt-in, because it needs Docker::

    EXAMLOPS_SPIRE_LIVE=1 .venv/bin/pytest tests/integration/test_serving_gateway_mtls_live.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from tests.unit._compose_yaml import load_compose

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "platform" / "infra" / "docker-compose"
IDENTITY = COMPOSE / "identity"
ENVOY = "envoyproxy/envoy:v1.39.1"
HELPER = "ghcr.io/spiffe/spiffe-helper:0.11.0"
PYTHON = "python:3.12-slim"
CURL = "curlimages/curl:8.16.0"
# The versions the generated stubs in serving/oip_grpc need at run time (see its __init__.py).
GRPC_PIP = "grpcio==1.80.0 protobuf==6.33.6"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("EXAMLOPS_SPIRE_LIVE") != "1", reason="set EXAMLOPS_SPIRE_LIVE=1"),
]

# The model server's stand-in, on loopback only: the sidecar in its network namespace is the one
# way in. It answers with what it received and from whom.
UPSTREAM = r"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    def _answer(self):
        n = int(self.headers.get("content-length") or 0)
        if n:
            self.rfile.read(n)
        body = json.dumps({"path": self.path, "peer": self.client_address[0],
                           "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = _answer
    def log_message(self, *a):
        pass
ThreadingHTTPServer(("127.0.0.1", 8001), H).serve_forever()
"""

# The model server's gRPC stand-in, beside it on loopback :8081 (the port the serving-mtls Envoy
# forwards gRPC to). It echoes the model, the verified tenant and the caller the hop verified.
GRPC_UPSTREAM = r"""
import sys
sys.path.insert(0, "/w")
from concurrent.futures import ThreadPoolExecutor
import grpc
from serving.oip_grpc import open_inference_grpc_pb2 as pb, open_inference_grpc_pb2_grpc as pbg
class Echo(pbg.GRPCInferenceServiceServicer):
    def ServerLive(self, request, context):
        return pb.ServerLiveResponse(live=True)
    def ModelInfer(self, request, context):
        md = dict(context.invocation_metadata())
        answer = pb.ModelInferResponse(model_name=request.model_name)
        for key in ("x-examlops-tenant", "x-forwarded-client-cert"):
            answer.parameters[key].string_param = md.get(key, "")
        return answer
server = grpc.server(ThreadPoolExecutor(max_workers=4))
pbg.add_GRPCInferenceServiceServicer_to_server(Echo(), server)
server.add_insecure_port("127.0.0.1:8081")
server.start()
print("grpc upstream up", flush=True)
server.wait_for_termination()
"""

# A one-shot spiffe-helper that writes the calling container's X.509-SVID for a probe client.
X509_HELPER = """
agent_address = "/run/spire/sockets/agent.sock"
cmd = ""
cert_dir = "/out"
daemon_mode = false
svid_file_name = "svid.pem"
svid_key_file_name = "svid_key.pem"
svid_bundle_file_name = "bundle.pem"
cert_file_mode = 0644
key_file_mode = 0644
"""

INFER = b'{"inputs": [{"name": "input-0", "shape": [1], "datatype": "FP64", "data": [1.0]}]}'

# Callers with an egress sidecar, by the identity their sidecar carries: an admin caller, an
# inference-only caller, and a registered workload that is no caller of the model server.
CALLERS = ("dashboard", "seanerbus-bridge", "autopilot")


def _hardening(service: str) -> list[str]:
    """`docker run` flags for the user, read-only root, capabilities and security options the
    identity overlay gives this service, so the test runs its Envoys exactly as Compose does. The
    first version ran them unhardened and missed that they could not start without capabilities."""
    overlay = load_compose(COMPOSE / "docker-compose.identity.yml")
    svc = overlay["services"][service]
    flags = ["--user", svc["user"]] if svc.get("user") else []
    flags += ["--read-only"] if svc.get("read_only") else []
    for cap in svc.get("cap_drop", []):
        flags += ["--cap-drop", cap]
    for opt in svc.get("security_opt", []):
        flags += ["--security-opt", opt]
    return flags


def _run(*args: str, check: bool = True, env: dict | None = None, timeout: float = 300):
    out = subprocess.run(list(args), capture_output=True, text=True, env=env, timeout=timeout)
    if check and out.returncode != 0:
        raise AssertionError(f"{' '.join(args[:5])}…: {out.stderr[-2000:]}")
    return out


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(url: str, *, key: str | None = None, body: bytes | None = None):
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    req.add_header("content-type", "application/json")
    if key:
        req.add_header("authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except OSError as exc:  # not listening yet, or still warming its SDS secrets
        return 0, str(exc).encode()


def _until(fn, timeout: float = 90.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = fn()
        if found:
            return found
        time.sleep(1)
    return None


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tag = uuid.uuid4().hex[:8]
    project, net = f"exa-mtls-{tag}", f"exa-mtls-net-{tag}"
    names = {k: f"exa-mtls-{k}-{tag}" for k in ("upstream", "sidecar", "gateway", "grpcup")}
    for caller in CALLERS:
        names[f"caller-{caller}"] = f"exa-mtls-caller-{caller}-{tag}"
        names[f"egress-{caller}"] = f"exa-mtls-egress-{caller}-{tag}"
    tmp = tmp_path_factory.mktemp("mtls")
    tmp.chmod(0o755)
    env = {**os.environ, "EXAMLOPS_IMAGE_PREFIX": project}
    compose = ("docker", "compose", "-f", str(IDENTITY / "compose.yml"), "-p", project)
    authz = None
    try:
        up = _run(*compose, "up", "-d", "--build", "--wait", "--wait-timeout", "180", env=env,
                  check=False)  # fmt: skip
        assert up.returncode == 0, up.stderr[-3000:]
        sockets = f"{project}_spire_agent_socket"
        _run("docker", "network", "create", net)
        gateway_ip = _run(
            "docker", "network", "inspect", net, "--format", "{{(index .IPAM.Config 0).Gateway}}"
        ).stdout.strip()  # fmt: skip

        # The authorization service with a virtual key, on the test network's gateway address only.
        db = tmp / "platform.db"
        os.environ["PLATFORM_DB"] = str(db)
        sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))
        from examlops import gateway as vkeys

        key = vkeys.issue_virtual_key("acme", "research", None, None, "test")
        authz_port = _port()
        authz_env = {**os.environ, "PLATFORM_DB": str(db)}
        authz_env.pop("EXAMLOPS_DB_BACKEND", None)
        authz_env["PYTHONPATH"] = str(ROOT / "platform" / "cli" / "src")
        authz = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "--factory", "examlops.serving_gateway:create_app",
             "--host", gateway_ip, "--port", str(authz_port), "--log-level", "warning"],
            env=authz_env,
        )  # fmt: skip

        (tmp / "upstream.py").write_text(UPSTREAM)
        (tmp / "grpc_upstream.py").write_text(GRPC_UPSTREAM)
        (tmp / "serving").mkdir()
        (tmp / "serving" / "__init__.py").write_text("")
        shutil.copytree(ROOT / "serving" / "oip_grpc", tmp / "serving" / "oip_grpc")
        for path in tmp.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        _run(
            "docker", "run", "-d", "--rm", "--name", names["upstream"], "--network", net,
            "--network-alias", "ray-serving", "-v", f"{tmp}:/w:ro", PYTHON, "python", "/w/upstream.py",
        )  # fmt: skip
        _run(
            "docker", "run", "-d", "--rm", "--name", names["sidecar"],
            "--network", f"container:{names['upstream']}", "--label", "examlops.spiffe=ray-serving",
            *_hardening("serving-mtls"),
            "-v", f"{sockets}:/run/spire/sockets:ro",
            "-v", f"{IDENTITY / 'serving-mtls-envoy.yaml'}:/etc/envoy/envoy.yaml:ro",
            ENVOY, "-c", "/etc/envoy/envoy.yaml", "--log-level", "warn",
        )  # fmt: skip
        # The model server's gRPC stand-in, in its network namespace on loopback :8081.
        _run(
            "docker", "run", "-d", "--rm", "--name", names["grpcup"],
            "--network", f"container:{names['upstream']}", "-v", f"{tmp}:/w:ro", PYTHON,
            "sh", "-c", f"pip install -q --no-cache-dir {GRPC_PIP} && python /w/grpc_upstream.py",
        )  # fmt: skip
        # The shipped mTLS gateway config, pointed only at this test's authorization service.
        config = (
            (COMPOSE / "gateway" / "envoy-mtls.yaml")
            .read_text()
            .replace("address: gateway-authz, port_value: 8090",
                     f"address: {gateway_ip}, port_value: {authz_port}")
            .replace("http://gateway-authz:8090", f"http://{gateway_ip}:{authz_port}")
        )  # fmt: skip
        (tmp / "envoy-mtls.yaml").write_text(config)
        (tmp / "envoy-mtls.yaml").chmod(0o644)
        gw_port = _port()
        _run(
            "docker", "run", "-d", "--rm", "--name", names["gateway"], "--network", net,
            "--label", "examlops.spiffe=gateway", "-p", f"127.0.0.1:{gw_port}:8080",
            "-v", f"{sockets}:/run/spire/sockets:ro", "-v", f"{tmp}:/cfg:ro",
            ENVOY, "-c", "/cfg/envoy-mtls.yaml", "--log-level", "warn",
        )  # fmt: skip
        # Each caller: a container, and the shipped egress sidecar in its network namespace.
        for caller in CALLERS:
            _run(
                "docker", "run", "-d", "--rm", "--name", names[f"caller-{caller}"],
                "--network", net, PYTHON, "sleep", "infinity",
            )  # fmt: skip
            _run(
                "docker", "run", "-d", "--rm", "--name", names[f"egress-{caller}"],
                "--network", f"container:{names[f'caller-{caller}']}",
                "--label", f"examlops.spiffe={caller}", *_hardening("serving-egress-dashboard"),
                "-v", f"{sockets}:/run/spire/sockets:ro",
                "-v", f"{IDENTITY / 'serving-egress-envoy.yaml'}:/etc/envoy/envoy.yaml:ro",
                ENVOY, "-c", "/etc/envoy/envoy.yaml", "--log-level", "warn",
            )  # fmt: skip
        base = f"http://127.0.0.1:{gw_port}"
        last: list = []

        def healthy() -> bool:
            last[:] = _call(base + "/v2/health/live")
            return last[0] == 200

        if not _until(healthy, timeout=120):
            stats = _run(
                "docker", "run", "--rm", "--network", f"container:{names['gateway']}", CURL, "-s",
                "localhost:9901/stats?filter=(ray_serving|ext_authz|gateway_authz)", check=False,
            ).stdout  # fmt: skip
            nonzero = [line for line in stats.splitlines() if not line.endswith(": 0")]
            raise AssertionError(f"gateway never healthy: {last} {nonzero[-40:]}")
        yield {
            "base": base, "key": key, "net": net, "sockets": sockets, "tmp": tmp, "names": names,
            "grpc": f"127.0.0.1:{gw_port}",
        }  # fmt: skip
    finally:
        _run("docker", "rm", "-f", *names.values(), check=False)
        _run("docker", "network", "rm", net, check=False)
        _run(*compose, "down", "-v", "--remove-orphans", check=False, env=env)
        _run("docker", "image", "rm", "-f", f"{project}-spire-init:latest", check=False)
        if authz is not None:
            authz.terminate()
            authz.wait(10)


def _svid_for(stack, label: str) -> Path:
    """The X.509-SVID SPIRE issues a container labelled examlops.spiffe=<label>."""
    out = Path(tempfile.mkdtemp(prefix=f"exa-mtls-{label}-"))
    out.chmod(0o777)
    conf = out / "helper.conf"
    conf.write_text(X509_HELPER)
    conf.chmod(0o644)
    _run(
        "docker", "run", "--rm", "--network", "none", "--label", f"examlops.spiffe={label}",
        "-v", f"{stack['sockets']}:/run/spire/sockets:ro", "-v", f"{out}:/out",
        "-v", f"{conf}:/helper.conf:ro", HELPER, "-config", "/helper.conf",
    )  # fmt: skip
    assert (out / "svid.pem").exists(), f"no SVID for {label}"
    return out


def _curl_serving_port(stack, *curl: str, mount: str | None = None):
    """curl from a container on the test network straight at the model server's TLS port."""
    volumes = ("-v", mount) if mount else ()
    return _run(
        "docker", "run", "--rm", "--network", stack["net"], *volumes, CURL,
        "-sk", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10", *curl,
        "https://ray-serving:8443/v2/health/live", check=False,
    )  # fmt: skip


def test_a_request_crosses_the_mtls_hop_and_the_model_server_learns_who_sent_it(stack):
    status, body = _call(stack["base"] + "/v2/models/jpcp/infer", key=stack["key"], body=INFER)
    assert status == 200, body
    seen = json.loads(body)
    assert seen["peer"] == "127.0.0.1"  # through the sidecar, over loopback
    assert seen["headers"]["x-examlops-tenant"] == "acme"  # the gateway's own work, unchanged
    xfcc = seen["headers"]["x-forwarded-client-cert"]
    assert "URI=spiffe://examlops.internal/gateway" in xfcc, xfcc


def test_the_gateways_own_checks_still_apply(stack):
    assert _call(stack["base"] + "/v2/models/jpcp/infer", body=INFER)[0] == 401


def test_a_client_without_a_certificate_is_refused(stack):
    out = _curl_serving_port(stack)
    assert out.returncode != 0 and out.stdout in ("", "000"), (out.returncode, out.stdout)


def test_only_a_serving_callers_identity_gets_through(stack):
    """A valid SVID of the trust domain is not enough: it must be one of the model server's
    callers. autopilot is registered with SPIRE (it calls the control plane) but not the model
    server."""
    for label, expected_ok in (("autopilot", False), ("gateway", True), ("dashboard", True)):
        svid = _svid_for(stack, label)
        out = _curl_serving_port(
            stack, "--cert", "/c/svid.pem", "--key", "/c/svid_key.pem", mount=f"{svid}:/c:ro"
        )
        if expected_ok:
            assert out.returncode == 0 and out.stdout == "200", (label, out.stdout, out.stderr)
        else:
            assert out.returncode != 0 and out.stdout in ("", "000"), (label, out.stdout)


def _from_caller(stack, caller: str, path: str, *, method: str = "GET", body: bytes | None = None):
    """(status, body) of a request the caller makes to its own loopback, where its sidecar is."""
    args = ["-s", "--path-as-is", "--max-time", "15", "-X", method, "-w", "\n%{http_code}"]
    if body is not None:
        args += ["-H", "content-type: application/json", "--data-binary", body.decode()]
    out = _run(
        "docker", "run", "--rm", "--network", f"container:{stack['names'][f'caller-{caller}']}",
        CURL, *args, f"http://127.0.0.1:8001{path}", check=False,
    )  # fmt: skip
    text, _, code = out.stdout.rpartition("\n")
    return int(code or 0), text


def _wait_for_caller(stack, caller: str) -> None:
    last: list = []

    def ready() -> bool:
        last[:] = _from_caller(stack, caller, "/v2/health/live")
        return last[0] == 200

    assert _until(ready, timeout=90), f"{caller}'s sidecar never reached the model server: {last}"


def test_a_caller_reaches_the_model_server_through_its_sidecar_under_its_own_identity(stack):
    _wait_for_caller(stack, "dashboard")
    status, text = _from_caller(
        stack, "dashboard", "/v2/models/jpcp/infer", method="POST", body=INFER
    )
    assert status == 200, text
    seen = json.loads(text)
    assert seen["peer"] == "127.0.0.1"  # through the model server's sidecar, over loopback
    xfcc = seen["headers"]["x-forwarded-client-cert"]
    assert "URI=spiffe://examlops.internal/dashboard" in xfcc, xfcc


def test_an_admin_caller_may_reload(stack):
    _wait_for_caller(stack, "dashboard")
    status, text = _from_caller(stack, "dashboard", "/reload", method="POST", body=b"{}")
    assert status == 200 and json.loads(text)["path"] == "/reload", (status, text)
    status, text = _from_caller(
        stack, "dashboard", "/infer-pipeline/traffic-rules/jpcp", method="POST", body=b"{}"
    )
    assert status == 200, (status, text)


def test_an_inference_only_caller_is_refused_every_spelling_of_an_admin_route(stack):
    _wait_for_caller(stack, "seanerbus-bridge")
    status, text = _from_caller(
        stack, "seanerbus-bridge", "/infer-pipeline/infer", method="POST", body=INFER
    )
    assert status == 200, text  # inference is what it is for
    xfcc = json.loads(text)["headers"]["x-forwarded-client-cert"]
    assert "URI=spiffe://examlops.internal/seanerbus-bridge" in xfcc, xfcc
    for path in ("/reload", "/reload/jpcp", "//reload", "/RELOAD", "/./reload", "/x/../reload",
                 "/infer-pipeline/traffic-rules/jpcp", "/infer-pipeline//traffic-rules/jpcp"):  # fmt: skip
        status, text = _from_caller(stack, "seanerbus-bridge", path, method="POST", body=b"{}")
        assert status == 403, (path, status, text)
    status, _ = _from_caller(stack, "seanerbus-bridge", "/%2Freload", method="POST", body=b"{}")
    assert status == 400  # an escaped slash is rejected before any rule reads the path


def test_a_sidecar_without_a_callers_identity_gets_nothing_through(stack):
    """autopilot's sidecar has a valid SVID; the model server refuses the handshake."""
    _wait_for_caller(stack, "dashboard")  # the model server's side is up
    status, text = _from_caller(stack, "autopilot", "/v2/health/live")
    assert status == 503, (status, text)
    assert "path" not in text  # the stand-in never answered


def test_the_gateways_identity_cannot_use_an_admin_route(stack):
    """The gateway routes only inference, and even straight at the TLS port its identity may not
    reload."""
    svid = _svid_for(stack, "gateway")
    out = _run(
        "docker", "run", "--rm", "--network", stack["net"], "-v", f"{svid}:/c:ro", CURL,
        "-sk", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10",
        "--cert", "/c/svid.pem", "--key", "/c/svid_key.pem", "-X", "POST",
        "https://ray-serving:8443/reload", check=False,
    )  # fmt: skip
    assert out.stdout == "403", (out.stdout, out.stderr)


def test_grpc_crosses_the_mtls_hop_over_http2_with_the_verified_identity(stack):
    """client ─h2c─▶ gateway ─mutual TLS, ALPN h2─▶ serving-mtls ─h2─▶ :8081 on the model server."""
    import grpc

    from serving.oip_grpc import open_inference_grpc_pb2 as pb
    from serving.oip_grpc import open_inference_grpc_pb2_grpc as pbg

    channel = grpc.insecure_channel(stack["grpc"])
    stub = pbg.GRPCInferenceServiceStub(channel)

    def live() -> bool:
        try:
            return stub.ServerLive(pb.ServerLiveRequest(), timeout=5).live
        except grpc.RpcError:
            return False

    with channel:
        assert _until(live, timeout=180), "the gRPC stand-in never answered through the gateway"
        answer = stub.ModelInfer(
            pb.ModelInferRequest(model_name="jpcp"),
            metadata=[("authorization", f"Bearer {stack['key']}"),
                      ("x-examlops-tenant", "someone-else")],
            timeout=10,
        )  # fmt: skip
    assert answer.model_name == "jpcp"
    assert answer.parameters["x-examlops-tenant"].string_param == "acme"  # verified, not claimed
    xfcc = answer.parameters["x-forwarded-client-cert"].string_param
    assert "URI=spiffe://examlops.internal/gateway" in xfcc, xfcc
