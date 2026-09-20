"""Every hop to the model server is mutual TLS under workload identities (ADR 0125 phases 2, 3).

`gateway/envoy-mtls.yaml` is what the gateway runs with `docker-compose.identity.yml`. It must stay
`gateway/envoy.yaml` in every other respect: same listeners, filters, routes and limits (the
serving-gateway guard holds those rules for envoy.yaml). The only differences are the upstream
cluster, which speaks mutual TLS with certificates from the SPIRE agent, the agent's SDS cluster,
and the node identity SDS needs.

The other callers (control plane, dashboard, agent, Dataplane bus bridge) each run
`identity/serving-egress-envoy.yaml` as a sidecar: plain HTTP in on their own loopback, the same
mutual TLS out. The serving side (`identity/serving-mtls-envoy.yaml`) accepts exactly those
identities, lets only the admin callers use the admin routes, and forwards over loopback to a model
server that listens on loopback alone. The live proof is
`tests/integration/test_serving_gateway_mtls_live.py`.
"""

from __future__ import annotations

import copy
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit._compose_yaml import load_compose

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "platform" / "infra" / "docker-compose"
PLAIN = COMPOSE / "gateway" / "envoy.yaml"
MTLS = COMPOSE / "gateway" / "envoy-mtls.yaml"
SERVING = COMPOSE / "identity" / "serving-mtls-envoy.yaml"
EGRESS = COMPOSE / "identity" / "serving-egress-envoy.yaml"
SOCKET = "/run/spire/sockets/agent.sock"
TLS = "envoy.transport_sockets.tls"
CALLERS = "^spiffe://[^/]+/(gateway|control-plane|dashboard|skipper|dataplane-bus-bridge)$"
ADMIN_CALLERS = "^spiffe://[^/]+/(control-plane|dashboard|skipper)$"


def _yaml(path: Path) -> dict:
    return load_compose(path)


def _clusters(config: dict) -> dict[str, dict]:
    return {c["name"]: c for c in config["static_resources"]["clusters"]}


def _sds(context: dict) -> tuple[str, str]:
    """(certificate SDS name, validation SDS name) and that both ask the SPIRE agent."""
    cert = context["tls_certificate_sds_secret_configs"]
    assert len(cert) == 1
    validation = context["combined_validation_context"]["validation_context_sds_secret_config"]
    for sds in (cert[0], validation):
        services = sds["sds_config"]["api_config_source"]["grpc_services"]
        assert services == [{"envoy_grpc": {"cluster_name": "spire_agent"}}]
    return cert[0]["name"], validation["name"]


def _peer_regex(context: dict) -> str:
    matchers = context["combined_validation_context"]["default_validation_context"][
        "match_typed_subject_alt_names"
    ]
    assert len(matchers) == 1 and matchers[0]["san_type"] == "URI"
    return matchers[0]["matcher"]["safe_regex"]["regex"]


def _spire_agent_cluster(config: dict) -> None:
    agent = _clusters(config)["spire_agent"]
    address = agent["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]
    assert address == {"pipe": {"path": SOCKET}}
    options = agent["typed_extension_protocol_options"][
        "envoy.extensions.upstreams.http.v3.HttpProtocolOptions"
    ]
    assert "http2_protocol_options" in options["explicit_http_config"]  # SDS is gRPC


def test_everything_but_the_upstream_hop_is_the_plain_gateway():
    plain, mtls = _yaml(PLAIN), _yaml(MTLS)
    assert mtls.pop("node")["id"]
    plain_clusters, mtls_clusters = _clusters(plain), _clusters(mtls)
    assert set(mtls_clusters) == set(plain_clusters) | {"spire_agent"}
    # The upstream clusters (REST and gRPC): the same, pointed at the sidecar's TLS port, plus
    # the TLS socket. Both hops go through serving-mtls on 8443; it tells them apart by path.
    for name, port in (("ray_serving", 8001), ("ray_serving_grpc", 8081)):
        upstream = copy.deepcopy(mtls_clusters[name])
        assert upstream.pop("transport_socket")["name"] == TLS, name
        endpoint = upstream["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]
        assert endpoint["address"]["socket_address"] == {
            "address": "ray-serving",
            "port_value": 8443,
        }
        endpoint["address"]["socket_address"]["port_value"] = port
        assert upstream == plain_clusters[name], name
    for name in set(plain_clusters) - {"ray_serving", "ray_serving_grpc"}:
        assert mtls_clusters[name] == plain_clusters[name], name
    # Listeners, filters, routes, limits, admin, overload manager: identical.
    for key in set(plain) | set(mtls):
        if key != "static_resources":
            assert mtls.get(key) == plain.get(key), key
    assert mtls["static_resources"]["listeners"] == plain["static_resources"]["listeners"]


def test_the_gateway_proves_itself_and_accepts_only_the_model_server():
    mtls = _yaml(MTLS)
    tls = _clusters(mtls)["ray_serving"]["transport_socket"]["typed_config"]
    assert tls["@type"].endswith("UpstreamTlsContext")
    context = tls["common_tls_context"]
    # Both bounds: an upstream context's default maximum is 1.2, so a 1.3 minimum alone leaves no
    # version (NO_SUPPORTED_VERSIONS_ENABLED; the live test found it).
    assert context["tls_params"] == {
        "tls_minimum_protocol_version": "TLSv1_3",
        "tls_maximum_protocol_version": "TLSv1_3",
    }
    assert _sds(context) == ("default", "ROOTCA")  # its own SVID; the trust domain's CA
    assert _peer_regex(context) == "^spiffe://[^/]+/ray-serving$"
    _spire_agent_cluster(mtls)


def test_the_model_server_accepts_its_callers_and_forwards_over_loopback():
    serving = _yaml(SERVING)
    assert serving["node"]["id"]
    (listener,) = serving["static_resources"]["listeners"]
    assert listener["address"]["socket_address"]["port_value"] == 8443
    (chain,) = listener["filter_chains"]
    tls = chain["transport_socket"]["typed_config"]
    assert tls["@type"].endswith("DownstreamTlsContext")
    assert tls["require_client_certificate"] is True
    context = tls["common_tls_context"]
    assert context["tls_params"] == {  # both bounds, as on the gateway's side
        "tls_minimum_protocol_version": "TLSv1_3",
        "tls_maximum_protocol_version": "TLSv1_3",
    }
    assert _sds(context) == ("default", "ROOTCA")
    assert _peer_regex(context) == CALLERS
    hcm = chain["filters"][0]["typed_config"]
    assert hcm["forward_client_cert_details"] == "SANITIZE_SET"  # a client's own XFCC is replaced
    routes = hcm["route_config"]["virtual_hosts"][0]["routes"]
    assert [r["route"]["cluster"] for r in routes] == [
        "ray_serving_grpc_local",
        "ray_serving_local",
    ]
    assert routes[0]["match"] == {"prefix": "/inference.GRPCInferenceService/", "grpc": {}}
    assert context["alpn_protocols"] == ["h2", "http/1.1"]  # HTTP/2 for the gateway's gRPC hop
    local = _clusters(serving)["ray_serving_local"]
    address = local["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]
    assert address["socket_address"] == {"address": "127.0.0.1", "port_value": 8001}
    grpc = _clusters(serving)["ray_serving_grpc_local"]
    address = grpc["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]
    assert address["socket_address"] == {"address": "127.0.0.1", "port_value": 8081}
    assert "admin" not in serving  # nothing listens but the mTLS port
    _spire_agent_cluster(serving)


@pytest.mark.parametrize(
    ("regex", "peer", "accepted"),
    [
        ("^spiffe://[^/]+/gateway$", "spiffe://examlops.internal/gateway", True),
        ("^spiffe://[^/]+/gateway$", "spiffe://another.domain/gateway", True),  # CA decides domain
        ("^spiffe://[^/]+/gateway$", "spiffe://examlops.internal/dashboard", False),
        ("^spiffe://[^/]+/gateway$", "spiffe://examlops.internal/ns/x/gateway", False),
        ("^spiffe://[^/]+/gateway$", "spiffe://examlops.internal/gateway/extra", False),
        ("^spiffe://[^/]+/ray-serving$", "spiffe://examlops.internal/ray-serving", True),
        ("^spiffe://[^/]+/ray-serving$", "spiffe://examlops.internal/ray-serving-x", False),
        (CALLERS, "spiffe://examlops.internal/dataplane-bus-bridge", True),
        (CALLERS, "spiffe://examlops.internal/skipper", True),
        (CALLERS, "spiffe://examlops.internal/autopilot", False),  # registered, but no caller
        (CALLERS, "spiffe://examlops.internal/dashboard-x", False),
        (CALLERS, "spiffe://examlops.internal/ns/x/dashboard", False),
        (ADMIN_CALLERS, "spiffe://examlops.internal/dashboard", True),
        (ADMIN_CALLERS, "spiffe://examlops.internal/gateway", False),
        (ADMIN_CALLERS, "spiffe://examlops.internal/dataplane-bus-bridge", False),
    ],
)
def test_the_peer_patterns_match_exactly_one_workload_path(regex, peer, accepted):
    """The pattern names the path; the CA from ROOTCA decides the trust domain."""
    assert bool(re.fullmatch(regex, peer)) is accepted


def test_the_compose_overlay_wires_both_sides():
    overlay = _yaml(COMPOSE / "docker-compose.identity.yml")["services"]
    gateway, serving = overlay["gateway"], overlay["serving-mtls"]
    assert gateway["labels"] == {"examlops.spiffe": "gateway"}
    assert "./gateway/envoy-mtls.yaml:/etc/envoy/envoy.yaml:ro" in gateway["volumes"]
    assert "spire_agent_socket:/run/spire/sockets:ro" in gateway["volumes"]
    assert serving["labels"] == {"examlops.spiffe": "ray-serving"}
    assert serving["network_mode"] == "service:ray-serving"
    assert "profiles" not in serving  # every caller needs it, not only the gateway
    assert "./identity/serving-mtls-envoy.yaml:/etc/envoy/envoy.yaml:ro" in serving["volumes"]
    base_gateway = _yaml(COMPOSE / "docker-compose.yml")["services"]["gateway"]
    assert serving["image"] == base_gateway["image"]  # one pinned Envoy
    assert serving["read_only"] is True and serving["cap_drop"] == ["ALL"]
    # Envoy's own user: as root without capabilities the image's entrypoint cannot chown its
    # stdout and the container exits (found live in phase 3; the earlier test ran it unhardened).
    assert serving["user"] == "101:101"
    workloads = _yaml(COMPOSE / "identity" / "compose.yml")["services"]["spire-register"][
        "environment"
    ]["SPIFFE_WORKLOADS"]
    assert {"gateway", "ray-serving"} <= set(
        re.sub(r"\$\{\w+:-([^}]*)\}", r"\1", workloads).split()
    )


# ── phase 3: the other callers, the admin rule and the loopback-only model server ──────────


def _rbac(serving: dict) -> dict:
    hcm = serving["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]
    filters = hcm["typed_config"]["http_filters"]
    assert [f["name"] for f in filters] == ["envoy.filters.http.rbac", "envoy.filters.http.router"]
    return filters[0]["typed_config"]["rules"]


def test_paths_are_canonical_before_the_admin_rule_reads_them():
    """`//reload`, `/./reload` and `/%2Freload` must not slip past a rule that matches on a path."""
    serving = _yaml(SERVING)
    hcm = serving["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]
    config = hcm["typed_config"]
    assert config["normalize_path"] is True and config["merge_slashes"] is True
    assert config["path_with_escaped_slashes_action"] == "REJECT_REQUEST"


def test_admin_routes_need_an_admin_caller():
    rules = _rbac(_yaml(SERVING))
    assert rules["action"] == "DENY"
    (policy,) = rules["policies"].values()
    (permission,) = policy["permissions"]
    prefixes = {r["url_path"]["path"]["prefix"] for r in permission["or_rules"]["rules"]}
    assert all(r["url_path"]["path"]["ignore_case"] for r in permission["or_rules"]["rules"])
    assert prefixes == {"/reload", "/infer-pipeline/traffic-rules/"}
    (principal,) = policy["principals"]
    admin = principal["not_id"]["authenticated"]["principal_name"]["safe_regex"]["regex"]
    assert admin == ADMIN_CALLERS


def _admin_paths() -> set[str]:
    """Every route the model server guards with its admin token, as Envoy sees the path."""
    paths = set()
    sources = {
        ROOT / "serving" / "ray_serving" / "app.py": "",
        ROOT / "serving" / "inference_pipeline" / "app.py": "/infer-pipeline",
    }
    for source, prefix in sources.items():
        text = source.read_text()
        for m in re.finditer(
            r'\.(?:post|put|patch|delete|get)\(\s*"([^"]+)"[^)]*require_serving_admin', text
        ):
            paths.add(prefix + m.group(1))
    return paths


def test_every_admin_route_of_the_model_server_is_behind_the_admin_rule():
    """A new admin route the rule does not cover would be open to the gateway and the bridge."""
    paths = _admin_paths()
    assert paths >= {"/reload", "/reload/{model_name}", "/infer-pipeline/traffic-rules/{model}"}
    permission = next(iter(_rbac(_yaml(SERVING))["policies"].values()))["permissions"][0]
    prefixes = [r["url_path"]["path"]["prefix"] for r in permission["or_rules"]["rules"]]
    uncovered = {p for p in paths if not any(p.lower().startswith(x) for x in prefixes)}
    assert not uncovered, uncovered


def test_a_callers_sidecar_listens_on_its_loopback_and_speaks_the_gateways_tls():
    egress = _yaml(EGRESS)
    assert egress["node"]["id"]
    (listener,) = egress["static_resources"]["listeners"]
    assert listener["address"]["socket_address"] == {"address": "127.0.0.1", "port_value": 8001}
    (chain,) = listener["filter_chains"]
    assert "transport_socket" not in chain  # plain HTTP in, from the caller's own namespace
    routes = chain["filters"][0]["typed_config"]["route_config"]["virtual_hosts"][0]["routes"]
    assert [r["route"] for r in routes] == [{"cluster": "ray_serving", "timeout": "0s"}]
    upstream = _clusters(egress)["ray_serving"]
    endpoint = upstream["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]
    assert endpoint["address"]["socket_address"] == {"address": "ray-serving", "port_value": 8443}
    # One TLS policy for every hop to the model server: the gateway's, byte for byte.
    gateway_tls = _clusters(_yaml(MTLS))["ray_serving"]["transport_socket"]
    assert upstream["transport_socket"] == gateway_tls
    assert "admin" not in egress
    _spire_agent_cluster(egress)


def _base_callers(base: dict) -> dict[str, dict]:
    """Services of the base stack that call the model server directly."""
    return {
        name: svc
        for name, svc in base["services"].items()
        if str((svc.get("environment") or {}).get("RAY_SERVE_URL", "")).startswith(
            "http://ray-serving:8001"
        )
    }


def test_every_direct_caller_goes_through_its_own_sidecar():
    """A service that starts calling ray-serving:8001 in the base stack fails here until it is
    wired: under the overlay nothing listens there for it."""
    base = _yaml(COMPOSE / "docker-compose.yml")
    overlay = _yaml(COMPOSE / "docker-compose.identity.yml")["services"]
    callers = _base_callers(base)
    assert set(callers) == {"control-plane", "dashboard", "agent", "dataplane-bus-bridge"}
    sidecars = {n: s for n, s in overlay.items() if n.startswith("serving-egress-")}
    assert set(sidecars) == {f"serving-egress-{c}" for c in callers}
    serving_image = overlay["serving-mtls"]["image"]
    labels = set()
    for name, svc in callers.items():
        sidecar = sidecars[f"serving-egress-{name}"]
        assert overlay[name]["environment"]["RAY_SERVE_URL"] == "http://127.0.0.1:8001", name
        assert sidecar["network_mode"] == f"service:{name}"
        assert sidecar.get("profiles") == svc.get("profiles"), name  # runs when its caller does
        assert sidecar["depends_on"][name]["restart"] is True  # a recreated caller takes it along
        assert sidecar["image"] == serving_image
        assert sidecar["read_only"] is True and sidecar["cap_drop"] == ["ALL"]
        assert sidecar["user"] == "101:101"
        assert "./identity/serving-egress-envoy.yaml:/etc/envoy/envoy.yaml:ro" in sidecar["volumes"]
        assert "spire_agent_socket:/run/spire/sockets:ro" in sidecar["volumes"]
        labels.add(sidecar["labels"]["examlops.spiffe"])
    # The model server accepts exactly the gateway and these identities: none missing, none stale.
    accepted = set(re.fullmatch(r"\^spiffe://\[\^/\]\+/\((.*)\)\$", CALLERS).group(1).split("|"))
    assert accepted == labels | {"gateway"}


def test_a_callers_sidecar_carries_the_identity_its_control_plane_credential_names():
    """The agent is `skipper` to the control plane; it must be `skipper` to the model server too."""
    overlay = _yaml(COMPOSE / "docker-compose.identity.yml")["services"]
    helpers = _yaml(COMPOSE / "identity" / "compose.yml")["services"]
    label_of_volume = {
        next(v.split(":")[0] for v in h["volumes"] if v.endswith(":/run/spire/svid")): h["labels"][
            "examlops.spiffe"
        ]
        for n, h in helpers.items()
        if n.startswith("spiffe-helper-")
    }
    for name, sidecar in overlay.items():
        if not name.startswith("serving-egress-"):
            continue
        caller = overlay[name.removeprefix("serving-egress-")]
        volume = next(v.split(":")[0] for v in caller["volumes"] if ":/run/spire/svid" in v)
        assert sidecar["labels"]["examlops.spiffe"] == label_of_volume[volume], name


def test_the_model_server_listens_on_loopback_and_its_port_is_not_published():
    base = _yaml(COMPOSE / "docker-compose.yml")["services"]["ray-serving"]
    overlay = _yaml(COMPOSE / "docker-compose.identity.yml")["services"]["ray-serving"]
    assert overlay["environment"]["RAY_SERVE_HOST"] == "127.0.0.1"
    # REST (8001) and gRPC (8081) both bind loopback under the overlay: neither may be published.
    kept = [p for p in base["ports"] if not str(p).endswith((":8001", ":8081"))]
    assert len(kept) == len(base["ports"]) - 2  # the base publishes each exactly once
    assert overlay["ports"] == kept  # everything else stays as the base has it


def test_the_model_server_binds_the_address_it_is_given(monkeypatch):
    for p in (str(ROOT), str(ROOT / "modelzoo")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from serving.ray_serving import app as rs_app

    started: list[dict] = []
    monkeypatch.setattr(rs_app.ray, "init", lambda **_kw: None)
    monkeypatch.setattr(rs_app.serve, "start", lambda **kw: started.append(kw["http_options"]))
    monkeypatch.setattr(rs_app.serve, "run", lambda *a, **kw: None)
    monkeypatch.setattr(rs_app.serve, "shutdown", lambda: None)
    monkeypatch.setattr(rs_app.ray, "shutdown", lambda: None)
    monkeypatch.setattr(rs_app, "_prepare_ray_environment", lambda: None)  # it edits os.environ
    monkeypatch.setattr(rs_app, "SERVE_HOST", "127.0.0.1")
    monkeypatch.setattr(rs_app, "GRPC_PORT", 0)  # gRPC has its own test (test_oip_grpc.py)
    # main() then waits for SIGINT/SIGTERM; deliver one on its first tick.
    handlers: dict = {}
    monkeypatch.setattr(rs_app.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))
    monkeypatch.setattr(
        rs_app.time,
        "sleep",
        lambda _s: handlers[rs_app.signal.SIGTERM](rs_app.signal.SIGTERM, None),
    )
    rs_app.main()
    assert started == [{"host": "127.0.0.1", "port": rs_app.SERVE_PORT}]


@pytest.mark.parametrize(
    ("value", "expected"), [(None, "0.0.0.0"), ("127.0.0.1", "127.0.0.1"), ("", "0.0.0.0")]
)
def test_the_bind_address_comes_from_ray_serve_host(value, expected):
    env = {k: v for k, v in __import__("os").environ.items() if k != "RAY_SERVE_HOST"}
    if value is not None:
        env["RAY_SERVE_HOST"] = value
    out = subprocess.run(
        [sys.executable, "-c", "from serving.ray_serving import app; print(app.SERVE_HOST)"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == expected


def test_the_gateways_grpc_hop_is_the_same_mutual_tls_over_http2():
    mtls = _yaml(MTLS)
    rest = _clusters(mtls)["ray_serving"]["transport_socket"]["typed_config"]["common_tls_context"]
    grpc = _clusters(mtls)["ray_serving_grpc"]["transport_socket"]["typed_config"][
        "common_tls_context"
    ]
    assert grpc.pop("alpn_protocols") == ["h2"]
    assert grpc == rest  # same TLS versions, same SVID, same peer check
