"""The serving gateway, live: real Envoy, the real authorization service, a stub model server.

Plan P4.4's acceptance: anonymous → 401, over quota → 429, oversize body → 413. Plus what makes a
gateway a gateway: an allowed request reaches the model server carrying the *verified* tenant (a
client's own ``X-ExaMLOps-Tenant`` is overwritten), admin routes are not reachable through it, and
the admin interface is not exposed.

Opt-in, because it needs Docker::

    EXAMLOPS_GATEWAY_LIVE=1 .venv/bin/pytest tests/integration/test_serving_gateway_live.py -v

Envoy runs with host networking against the committed ``envoy.yaml``, rewritten only to point at
the ports this test chose.
"""

from __future__ import annotations

import json
import os
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

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("EXAMLOPS_GATEWAY_LIVE") != "1", reason="set EXAMLOPS_GATEWAY_LIVE=1"
    ),
]


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Upstream(BaseHTTPRequestHandler):
    """The model server's stand-in: answers every request with the headers it received."""

    def _answer(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        body = json.dumps(
            {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}}
        )
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    do_GET = do_POST = _answer

    def log_message(self, *args) -> None:  # noqa: D401 - quiet
        return


def _grpc_upstream():
    """The model server's gRPC stand-in: echoes the model and the identity metadata it received."""
    from concurrent.futures import ThreadPoolExecutor

    import grpc

    sys.path.insert(0, str(ROOT))
    from serving.oip_grpc import open_inference_grpc_pb2 as pb
    from serving.oip_grpc import open_inference_grpc_pb2_grpc as pbg

    class Echo(pbg.GRPCInferenceServiceServicer):
        def ServerLive(self, request, context):  # noqa: N802
            return pb.ServerLiveResponse(live=True)

        def ModelInfer(self, request, context):  # noqa: N802
            metadata = dict(context.invocation_metadata())
            answer = pb.ModelInferResponse(model_name=request.model_name)
            for key in ("x-examlops-tenant", "x-examlops-project", "x-examlops-principal"):
                answer.parameters[key].string_param = metadata.get(key, "")
            return answer

    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    pbg.add_GRPCInferenceServiceServicer_to_server(Echo(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return server, port


def _wait(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return  # it answered
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(url)


@pytest.fixture(scope="module")
def gateway(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gateway")
    db = tmp / "platform.db"
    env = {
        **os.environ,
        "PLATFORM_DB": str(db),
        "EXAMLOPS_GATEWAY_TENANT_RPM": "4",
        "EXAMLOPS_MULTITENANCY": "1",  # project scope at serve time (plan P4.9)
    }
    env.pop("EXAMLOPS_DB_BACKEND", None)
    os.environ["PLATFORM_DB"] = str(db)
    sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))
    from examlops import gateway as vkeys
    from examlops.data.projects import assign_model_to_project, create_project

    create_project("vault")
    assign_model_to_project("vault", "FRAUD")  # the registry spelling; callers say `fraud`
    keys = {
        "acme": vkeys.issue_virtual_key("acme", "research", None, None, "test"),
        "narrow": vkeys.issue_virtual_key("acme", "research", ["other"], None, "test"),
        "vault": vkeys.issue_virtual_key("globex", "vault", None, None, "test"),
    }

    upstream_port, authz_port, gw_port, metrics_port, admin_port = (_port() for _ in range(5))
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), _Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    grpc_upstream, grpc_port = _grpc_upstream()

    authz = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            "examlops.serving_gateway:create_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(authz_port),
            "--log-level",
            "warning",
        ],
        env={**env, "PYTHONPATH": str(ROOT / "platform" / "cli" / "src")},
    )
    config = (
        CONFIG.read_text()
        .replace(
            "address: ray-serving, port_value: 8001",
            f"address: 127.0.0.1, port_value: {upstream_port}",
        )
        .replace(
            "address: ray-serving, port_value: 8081",
            f"address: 127.0.0.1, port_value: {grpc_port}",
        )
        .replace(
            "address: gateway-authz, port_value: 8090",
            f"address: 127.0.0.1, port_value: {authz_port}",
        )
        .replace("http://gateway-authz:8090", f"http://127.0.0.1:{authz_port}")
        .replace("port_value: 8080", f"port_value: {gw_port}")
        .replace("port_value: 9902", f"port_value: {metrics_port}")
        .replace("port_value: 9901", f"port_value: {admin_port}")
    )
    (tmp / "envoy.yaml").write_text(config)
    # Envoy runs as a non-root user in its image; pytest's tmp dirs are 0700.
    tmp.chmod(0o755)
    (tmp / "envoy.yaml").chmod(0o644)
    name = f"examlops-gw-test-{gw_port}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--network",
            "host",
            "-v",
            f"{tmp}:/cfg:ro",
            ENVOY_IMAGE,
            "-c",
            "/cfg/envoy.yaml",
            "--log-level",
            "warn",
        ],
        check=True,
        capture_output=True,
    )
    try:
        _wait(f"http://127.0.0.1:{authz_port}/healthz")
        _wait(f"http://127.0.0.1:{gw_port}/v2/health/live")
        yield {
            "base": f"http://127.0.0.1:{gw_port}",
            "metrics": f"http://127.0.0.1:{metrics_port}",
            "keys": keys,
            "authz": authz,
            "db": str(db),
            "grpc": f"127.0.0.1:{gw_port}",
        }
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        authz.terminate()
        authz.wait(10)
        upstream.shutdown()
        grpc_upstream.stop(0)


def _call(
    url: str, *, key: str | None = None, body: bytes | None = None, headers=None, method=None
):
    req = urllib.request.Request(
        url, data=body, method=method or ("POST" if body is not None else "GET")
    )
    req.add_header("content-type", "application/json")
    if key:
        req.add_header("authorization", f"Bearer {key}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


INFER = b'{"inputs": [{"name": "input-0", "shape": [1], "datatype": "FP64", "data": [1.0]}]}'


def test_anonymous_is_refused_with_401(gateway):
    status, headers, body = _call(gateway["base"] + "/v2/models/jpcp/infer", body=INFER)
    assert status == 401
    assert "bearer" in headers.get("www-authenticate", "").lower()
    assert "error" in json.loads(body)


def test_health_is_open(gateway):
    assert _call(gateway["base"] + "/v2/health/ready")[0] == 200


def test_an_allowed_request_carries_the_verified_tenant_not_the_clients(gateway):
    status, _, body = _call(
        gateway["base"] + "/v2/models/jpcp/infer",
        key=gateway["keys"]["acme"],
        body=INFER,
        headers={"x-examlops-tenant": "someone-else", "x-examlops-project": "theirs"},
    )
    assert status == 200, body
    seen = json.loads(body)["headers"]
    assert seen["x-examlops-tenant"] == "acme" and seen["x-examlops-project"] == "research"
    assert seen["x-examlops-principal"].startswith("key:")


def test_a_key_outside_its_allow_list_is_403(gateway):
    status, _, body = _call(
        gateway["base"] + "/v2/models/jpcp/infer", key=gateway["keys"]["narrow"], body=INFER
    )
    assert status == 403 and "allow-listed" in json.loads(body)["error"]


def test_an_unknown_key_is_401(gateway):
    fake = "-".join(("exa", "not", "a", "real", "key"))
    assert _call(gateway["base"] + "/v2/models/jpcp/infer", key=fake, body=INFER)[0] == 401


def test_an_oversize_body_is_413(gateway):
    huge = b'{"inputs": "' + b"x" * (9 * 1024 * 1024) + b'"}'
    assert (
        _call(gateway["base"] + "/v2/models/jpcp/infer", key=gateway["keys"]["acme"], body=huge)[0]
        == 413
    )


def test_admin_routes_are_not_served(gateway):
    for path in ("/reload", "/reload/jpcp", "/infer-pipeline/traffic-rules/jpcp", "/metrics"):
        status = _call(gateway["base"] + path, key=gateway["keys"]["acme"], body=b"{}")[0]
        assert status == 404, path


def test_the_admin_interface_is_not_exposed(gateway):
    assert _call(gateway["metrics"] + "/stats/prometheus")[0] == 200
    assert _call(gateway["metrics"] + "/quitquitquit", body=b"")[0] == 404


def test_a_cross_project_request_is_denied(gateway):
    """Plan P4.9: a model owned by one project is refused to another project's key."""
    url = gateway["base"] + "/v2/models/fraud/infer"
    status, _, body = _call(url, key=gateway["keys"]["acme"], body=INFER)
    assert status == 403 and "belongs to project 'vault'" in json.loads(body)["error"]
    status, _, body = _call(url, key=gateway["keys"]["vault"], body=INFER)
    assert status == 200
    seen = json.loads(body)["headers"]
    assert seen["x-examlops-tenant"] == "globex" and seen["x-examlops-project"] == "vault"


def test_over_the_tenant_quota_is_429(gateway):
    """EXAMLOPS_GATEWAY_TENANT_RPM=4 here; the earlier tests already spent part of it."""
    statuses = [
        _call(gateway["base"] + "/v2/models/jpcp/infer", key=gateway["keys"]["acme"], body=INFER)
        for _ in range(6)
    ]
    assert statuses[-1][0] == 429
    assert statuses[-1][1].get("retry-after") == "60"


def _exa(gateway, tmp_path, *args: str, token: str | None) -> dict:
    """Run the real `exa` with `ray_serve` pointed at the gateway, and its JSON answer."""
    env = {
        **os.environ,
        "EXAMLOPS_CONFIG": str(tmp_path / "config.toml"),
        "RAY_SERVE_URL": gateway["base"],
        "PLATFORM_DB": str(tmp_path / "cli.db"),
        "PYTHONPATH": str(ROOT / "platform" / "cli" / "src"),
    }
    env.pop("EXAMLOPS_SERVING_TOKEN", None)
    env.pop("EXAMLOPS_LOADTEST_TOKEN", None)
    if token:
        env["EXAMLOPS_SERVING_TOKEN"] = token
    out = subprocess.run(
        [sys.executable, "-m", "examlops.cli", "--json", *args],
        capture_output=True, text=True, env=env, timeout=120,
    )  # fmt: skip
    return json.loads(out.stdout)


def test_exa_reaches_the_model_server_through_the_gateway_with_its_serving_token(
    gateway, tmp_path, monkeypatch
):
    """`exa` on a host where only the gateway is reachable (the workload-identity overlay): its
    configured serving token is what gets it through. Its own tenant, so the quota tests above
    do not interfere."""
    from examlops import gateway as vkeys

    monkeypatch.setenv("PLATFORM_DB", gateway["db"])  # the store the authorization service reads
    key = vkeys.issue_virtual_key("exaco", "research", None, None, "test")
    body = tmp_path / "body.json"
    body.write_text(INFER.decode())
    run = ("serve", "loadtest", "jpcp", "--body", str(body), "--rate", "1", "--duration", "2")

    with_key = _exa(gateway, tmp_path, *run, token=key)
    assert with_key["statuses"] == {"200": with_key["sent"]} and with_key["sent"] >= 1, with_key
    without = _exa(gateway, tmp_path, *run, token=None)
    assert without["statuses"] == {"401": without["sent"]}, without


def _grpc_stub(gateway):
    import grpc

    from serving.oip_grpc import open_inference_grpc_pb2_grpc as pbg

    channel = grpc.insecure_channel(gateway["grpc"])
    return channel, pbg.GRPCInferenceServiceStub(channel)


def test_grpc_health_is_open_through_the_gateway(gateway):
    from serving.oip_grpc import open_inference_grpc_pb2 as pb

    channel, stub = _grpc_stub(gateway)
    with channel:
        assert stub.ServerLive(pb.ServerLiveRequest(), timeout=10).live is True


def test_grpc_without_a_credential_is_unauthenticated(gateway):
    import grpc

    from serving.oip_grpc import open_inference_grpc_pb2 as pb

    channel, stub = _grpc_stub(gateway)
    with channel, pytest.raises(grpc.RpcError) as caught:
        stub.ModelInfer(pb.ModelInferRequest(model_name="jpcp"), timeout=10)
    assert caught.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_grpc_inference_is_refused_under_multitenancy(gateway):
    """This gateway runs with multi-tenancy on: gRPC names its model in the body, which the
    gateway never reads, so it cannot be scoped and is refused, never waved through."""
    import grpc

    from serving.oip_grpc import open_inference_grpc_pb2 as pb

    channel, stub = _grpc_stub(gateway)
    metadata = [("authorization", f"Bearer {gateway['keys']['acme']}")]
    with channel, pytest.raises(grpc.RpcError) as caught:
        stub.ModelInfer(pb.ModelInferRequest(model_name="jpcp"), metadata=metadata, timeout=10)
    assert caught.value.code() == grpc.StatusCode.PERMISSION_DENIED
    assert "gRPC cannot be authorized" in (caught.value.details() or "")


def test_an_unrouted_grpc_service_is_unimplemented_and_spends_nothing(gateway):
    import grpc

    channel = grpc.insecure_channel(gateway["grpc"])
    other = channel.unary_unary("/grpc.health.v1.Health/Check")
    with channel, pytest.raises(grpc.RpcError) as caught:
        other(b"", timeout=10)
    assert caught.value.code() == grpc.StatusCode.UNIMPLEMENTED  # the gateway's 404


def _stat(gateway, name: str) -> float:
    body = _call(gateway["metrics"] + "/stats/prometheus")[2].decode()
    values = [
        float(line.rsplit(" ", 1)[1])
        for line in body.splitlines()
        if line.startswith(name + "{") and 'envoy_http_conn_manager_prefix="serving"' in line
    ]
    assert values, f"{name} is not exported; the gateway alerts select on it"
    return sum(values)


def test_zz_with_authorization_down_requests_fail_closed_and_are_counted(gateway):
    """Last, because it stops the authorization service. The alert
    ServingGatewayAuthorizationFailing reads this counter; its name comes from the real Envoy."""
    before = _stat(gateway, "envoy_http_ext_authz_error")
    gateway["authz"].terminate()
    gateway["authz"].wait(10)
    status, _, _ = _call(
        gateway["base"] + "/v2/models/jpcp/infer", key=gateway["keys"]["acme"], body=INFER
    )
    assert status == 503  # fail closed: no decision, no inference
    assert _stat(gateway, "envoy_http_ext_authz_error") > before
