"""The serving gateway's Envoy configuration keeps its security properties (plan P4.4, ADR 0126).

A proxy config drifts one convenient edit at a time: a route added "just for debugging", the admin
interface bound to all interfaces, ``failure_mode_allow`` flipped so an authorization outage lets
everything through. Each rule here is one of those, checked statically against
``platform/infra/docker-compose/gateway/envoy.yaml``; the live test runs the file under real Envoy.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from examlops.serving_gateway import IDENTITY_HEADERS

ROOT = Path(__file__).resolve().parents[2]
D = ROOT / "platform" / "infra" / "docker-compose"
CONFIG = yaml.safe_load((D / "gateway" / "envoy.yaml").read_text(encoding="utf-8"))
COMPOSE = yaml.safe_load((D / "docker-compose.yml").read_text(encoding="utf-8"))
LIVE_TEST = (ROOT / "tests" / "integration" / "test_serving_gateway_live.py").read_text()

SERVED = {
    ("prefix", "/v2/"),
    ("path", "/v2"),
    ("path", "/infer-pipeline/infer"),
    ("prefix", "/predict/"),
    ("prefix", "/inference.GRPCInferenceService/"),  # the same protocol over gRPC
    ("grpc", ()),
}
GRPC_PREFIX = "/inference.GRPCInferenceService/"


def _listener(name: str) -> dict:
    return next(x for x in CONFIG["static_resources"]["listeners"] if x["name"] == name)


def _hcm(name: str) -> dict:
    return _listener(name)["filter_chains"][0]["filters"][0]["typed_config"]


def _filters() -> list[dict]:
    return _hcm("serving")["http_filters"]


def _filter(short: str) -> dict:
    return next(f for f in _filters() if f["name"] == f"envoy.filters.http.{short}")["typed_config"]


def test_the_filter_chain_is_ceiling_then_body_limit_then_authorization_then_router():
    names = [f["name"].rsplit(".", 1)[1] for f in _filters()]
    assert names == ["local_ratelimit", "buffer", "ext_authz", "router"]


def test_an_authorization_outage_fails_closed():
    authz = _filter("ext_authz")
    assert authz["failure_mode_allow"] is False
    assert authz["status_on_error"]["code"] == 503


def test_only_the_credential_goes_to_the_authorization_service():
    assert _filter("ext_authz")["allowed_headers"]["patterns"] == [{"exact": "authorization"}]


def test_the_identity_headers_the_service_sets_are_the_ones_forwarded_upstream():
    """Envoy overwrites a client's copy only for headers named here, so the list must match."""
    forwarded = _filter("ext_authz")["http_service"]["authorization_response"][
        "allowed_upstream_headers"
    ]
    assert {p["exact"] for p in forwarded["patterns"]} == set(IDENTITY_HEADERS)


def test_request_bodies_are_bounded():
    assert 0 < _filter("buffer")["max_request_bytes"] <= 64 * 1024 * 1024


def test_only_inference_is_routed_and_everything_else_is_404_without_authorization():
    routes = _hcm("serving")["route_config"]["virtual_hosts"][0]["routes"]
    served = {(k, tuple(v) if isinstance(v, dict) else v) for r in routes[:-1]
              for k, v in r["match"].items()}  # fmt: skip
    assert served == SERVED
    for route in routes[:-1]:
        grpc = route["match"].get("prefix") == GRPC_PREFIX
        assert route["route"]["cluster"] == ("ray_serving_grpc" if grpc else "ray_serving")
        if grpc:
            assert route["match"]["grpc"] == {}  # only real gRPC requests, by content-type
    catch_all = routes[-1]
    assert catch_all["match"] == {"prefix": "/"} and catch_all["direct_response"]["status"] == 404
    per_route = catch_all["typed_per_filter_config"]["envoy.filters.http.ext_authz"]
    assert per_route["disabled"] is True


def test_a_model_server_that_went_away_mid_request_is_retried():
    """What makes pod churn invisible to a caller.

    A Kubernetes Service balances connections, so a pod that stops closes the keep-alive
    connections pinned to it and whatever they were carrying is cut. The Kubernetes serving drill
    (`tests/integration/test_serving_kind_drill_live.py`) measured single-digit losses out of tens
    of thousands of requests from exactly that, and zero for a caller that retries once — which the
    gateway does on every inference route on the caller's behalf. Without `reset` in this list, a
    rolling upgrade of the model server becomes visible to every client of the platform.
    """
    routes = _hcm("serving")["route_config"]["virtual_hosts"][0]["routes"]
    policies = [
        (r["match"], r["route"]["retry_policy"])
        for r in routes
        if "route" in r and "retry_policy" in r["route"]
    ]
    assert policies, "no inference route retries anything"
    for match, policy in policies:  # REST and gRPC alike: the pod dies the same way for both
        assert "reset" in policy["retry_on"], (match, policy)
        assert "connect-failure" in policy["retry_on"], (match, policy)
        assert policy["num_retries"] >= 1, (match, policy)


def test_the_model_server_cluster_stops_choosing_an_endpoint_that_stops_answering():
    """A pod that accepts connections and never answers is the expensive failure, because every
    request sent there costs the caller its whole deadline rather than failing fast.

    Kubernetes takes about two minutes to stop routing to it (`test_serving_node_loss_kind_live.py`);
    the gateway takes about ten seconds, measured in
    `tests/integration/test_serving_gateway_ejection_live.py` — 13 of 20 requests timed out with
    these blocks removed, none with them. The numbers below are what produce that: two failed probes
    five seconds apart.
    """
    cluster = next(c for c in CONFIG["static_resources"]["clusters"] if c["name"] == "ray_serving")
    check = cluster["health_checks"][0]
    assert check["http_health_check"]["path"] == "/ready"
    assert check["interval"] == "5s" and check["unhealthy_threshold"] == 2
    assert check["healthy_threshold"] >= 1, "an ejected endpoint must be able to come back"
    # Never eject everything: with one endpoint left, a "healthy" one is what the callers have.
    for name in ("ray_serving", "ray_serving_grpc"):
        outlier = next(c for c in CONFIG["static_resources"]["clusters"] if c["name"] == name)[
            "outlier_detection"
        ]
        assert outlier["max_ejection_percent"] <= 50, name
        # A hang is a local-origin failure; without this split it is not counted at all.
        assert outlier["split_external_local_origin_errors"] is True, name
        assert outlier["consecutive_local_origin_failure"] >= 1, name


def test_retries_are_bounded_by_a_budget():
    route = _hcm("serving")["route_config"]["virtual_hosts"][0]["routes"][0]["route"]
    assert route["retry_policy"]["num_retries"] <= 3
    cluster = next(c for c in CONFIG["static_resources"]["clusters"] if c["name"] == "ray_serving")
    budget = cluster["circuit_breakers"]["thresholds"][0]["retry_budget"]
    assert 0 < budget["budget_percent"]["value"] <= 25


def test_the_admin_interface_is_loopback_only_and_metrics_expose_one_path():
    assert CONFIG["admin"]["address"]["socket_address"]["address"] == "127.0.0.1"
    routes = _hcm("metrics")["route_config"]["virtual_hosts"][0]["routes"]
    proxied = [r for r in routes if "route" in r]
    assert [r["match"] for r in proxied] == [{"path": "/stats/prometheus"}]


def test_the_compose_image_is_pinned_to_the_version_the_live_test_runs():
    image = COMPOSE["services"]["gateway"]["image"]
    assert re.fullmatch(r"envoyproxy/envoy:v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", image), image
    version = image.split(":")[1].split("@")[0]
    assert f'ENVOY_IMAGE = "envoyproxy/envoy:{version}"' in LIVE_TEST


def test_the_gateway_is_opt_in():
    for service in ("gateway", "gateway-authz"):
        assert COMPOSE["services"][service]["profiles"] == ["gateway"]


def test_grpc_reaches_the_model_servers_grpc_port_over_http2_within_the_same_limits():
    """One listener for both forms: the same ceiling, body limit and authorization run first."""
    hcm = _hcm("serving")
    assert hcm["codec_type"] == "AUTO"  # HTTP/1.1 and cleartext HTTP/2 on one port
    route = next(
        r for r in hcm["route_config"]["virtual_hosts"][0]["routes"]
        if r["match"].get("prefix") == GRPC_PREFIX
    )["route"]  # fmt: skip
    assert route["max_stream_duration"]["grpc_timeout_header_max"] == route["timeout"]
    assert route["retry_policy"]["num_retries"] <= 3
    assert "unavailable" in route["retry_policy"]["retry_on"]
    cluster = next(
        c for c in CONFIG["static_resources"]["clusters"] if c["name"] == "ray_serving_grpc"
    )
    options = cluster["typed_extension_protocol_options"][
        "envoy.extensions.upstreams.http.v3.HttpProtocolOptions"
    ]
    assert "http2_protocol_options" in options["explicit_http_config"]
    address = cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]
    assert address["socket_address"] == {"address": "ray-serving", "port_value": 8081}
    budget = cluster["circuit_breakers"]["thresholds"][0]["retry_budget"]
    assert 0 < budget["budget_percent"]["value"] <= 25
