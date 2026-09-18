"""The chart deploys the serving gateway (plan P4.4 / P4.11, ADR 0126).

With ``gateway.enabled`` the chart runs what Compose runs under the ``gateway`` profile:
- Envoy, as the one front door to the serving plane;
- the authorization service `examlops.serving_gateway`, which checks virtual keys and IdP tokens,
  applies per-tenant quotas and sets the verified tenant headers.

The Envoy configuration is not a second copy to keep in step. The chart carries
`files/gateway-envoy.yaml`, byte-identical to Compose's `gateway/envoy.yaml`, and renders it with
only the three service addresses substituted. Both tiers get the chart's hardening, a
PodDisruptionBudget, their own NetworkPolicy tiers, and optionally an ingress host and a
ServiceMonitor for Envoy's statistics.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "platform" / "infra" / "helm" / "examlops"
COMPOSE_ENVOY = ROOT / "platform" / "infra" / "docker-compose" / "gateway" / "envoy.yaml"
HELM = shutil.which("helm") or shutil.which("helm", path=str(Path.home() / ".local" / "bin"))
REGISTRY = ("--set", "global.imageRegistry=ghcr.io/mskazemi/")
ON = ("--set", "gateway.enabled=true")

pytestmark = pytest.mark.skipif(HELM is None, reason="helm is not installed")


def _helm(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [HELM, "template", "rel", str(CHART), "--namespace", "mlops", *REGISTRY, *extra],
        capture_output=True,
        text=True,
    )


def _docs(*extra: str) -> list[dict]:
    out = _helm(*extra)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _of(kind: str, docs: list[dict]) -> dict[str, dict]:
    return {
        d["metadata"]["labels"].get("app.kubernetes.io/component", d["metadata"]["name"]): d
        for d in docs
        if d["kind"] == kind
    }


def _pod(deployment: dict) -> dict:
    return deployment["spec"]["template"]["spec"]


def test_off_by_default():
    docs = _docs()
    assert not {"gateway", "gateway-authz"} & set(_of("Deployment", docs))
    assert not [d for d in docs if d["metadata"]["name"].endswith("-gateway-envoy")]


def test_the_chart_ships_composes_envoy_config_byte_for_byte():
    """One configuration: a change to the Compose gateway reaches the chart, or this fails."""
    assert (CHART / "files" / "gateway-envoy.yaml").read_bytes() == COMPOSE_ENVOY.read_bytes(), (
        "the chart's gateway config differs from Compose's. After changing gateway/envoy.yaml: "
        "cp platform/infra/docker-compose/gateway/envoy.yaml "
        "platform/infra/helm/examlops/files/gateway-envoy.yaml"
    )


def test_the_rendered_config_differs_only_in_service_addresses():
    docs = _docs(
        *ON,
        "--set",
        "gateway.upstream.host=ray-serve.serving",
        "--set",
        "gateway.upstream.port=8000",
    )
    config = next(
        d
        for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "rel-examlops-gateway-envoy"
    )
    rendered = config["data"]["envoy.yaml"]
    expected = (
        COMPOSE_ENVOY.read_text()
        .replace(
            "address: ray-serving, port_value: 8001", "address: ray-serve.serving, port_value: 8000"
        )
        .replace(  # the gRPC upstream follows the host; its port is upstream.grpcPort
            "address: ray-serving, port_value: 8081", "address: ray-serve.serving, port_value: 8081"
        )
        .replace(
            "address: gateway-authz, port_value: 8090",
            "address: rel-examlops-gateway-authz, port_value: 8090",
        )
        .replace("uri: http://gateway-authz:8090", "uri: http://rel-examlops-gateway-authz:8090")
    )
    assert rendered == expected
    assert "gateway-authz:8090" not in rendered.replace("rel-examlops-gateway-authz", "")


def test_both_tiers_are_hardened_and_highly_available():
    docs = _docs(*ON)
    deployments = _of("Deployment", docs)
    for tier in ("gateway", "gateway-authz"):
        pod = _pod(deployments[tier])
        assert pod["securityContext"]["runAsNonRoot"] is True
        (container,) = pod["containers"]
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
        assert container["readinessProbe"] and container["livenessProbe"]
        assert deployments[tier]["spec"]["replicas"] == 2
    pdbs = _of("PodDisruptionBudget", docs)
    assert {"gateway", "gateway-authz"} <= set(pdbs)
    envoy = _pod(deployments["gateway"])["containers"][0]
    assert envoy["image"].startswith("envoyproxy/envoy:v1.39.1@sha256:")
    assert "--disable-hot-restart" in envoy["args"]  # no shared memory on a read-only pod


def test_the_authz_tier_runs_the_platforms_authorization_service():
    docs = _docs(*ON, "--set", "gateway.tenantRpm=120")
    authz = _pod(_of("Deployment", docs)["gateway-authz"])["containers"][0]
    assert (
        authz["image"] == "ghcr.io/mskazemi/examlops-control-plane:" + authz["image"].split(":")[-1]
    )
    assert authz["command"][:3] == ["uvicorn", "--factory", "examlops.serving_gateway:create_app"]
    env = {e["name"]: e.get("value") for e in authz.get("env", [])}
    assert env["EXAMLOPS_GATEWAY_TENANT_RPM"] == "120"
    assert env["EXAMLOPS_ACTOR"] == "serving-gateway"
    sources = [next(iter(s)) for s in authz["envFrom"]]
    assert sources == ["configMapRef", "secretRef"]  # the datastore DSN, like the control plane


def test_services_and_the_optional_ingress_host():
    docs = _docs(*ON, "--set", "gateway.ingress.host=inference.example.org")
    services = _of("Service", docs)
    assert {p["port"] for p in services["gateway"]["spec"]["ports"]} == {8080, 9902}
    assert {p["port"] for p in services["gateway-authz"]["spec"]["ports"]} == {8090}
    ingress = next(d for d in docs if d["kind"] == "Ingress")
    hosts = {r["host"]: r for r in ingress["spec"]["rules"]}
    backend = hosts["inference.example.org"]["http"]["paths"][0]["backend"]["service"]
    assert backend == {"name": "rel-examlops-gateway", "port": {"name": "http"}}
    assert "inference.example.org" in ingress["spec"]["tls"][0]["hosts"]
    no_host = next(d for d in _docs(*ON) if d["kind"] == "Ingress")
    assert "inference.example.org" not in {r["host"] for r in no_host["spec"]["rules"]}


def test_network_policies_let_only_the_gateway_reach_authorization():
    docs = _docs(*ON, "--set", "networkPolicy.enabled=true")
    policies = _of("NetworkPolicy", docs)
    authz = policies["gateway-authz"]["spec"]
    (rule,) = authz["ingress"]
    selector = rule["from"][0]["podSelector"]["matchLabels"]
    assert selector["app.kubernetes.io/component"] == "gateway"
    assert rule["ports"] == [{"protocol": "TCP", "port": 8090}]
    gateway = policies["gateway"]["spec"]
    egress_ports = {p["port"] for r in gateway["egress"] for p in r.get("ports", [])}
    assert {8090, 8001, 8081} <= egress_ports  # authorization; the model server's REST and gRPC
    ingress_ports = {p["port"] for r in gateway["ingress"] for p in r.get("ports", [])}
    assert ingress_ports == {8080, 9902}  # clients through the ingress; Prometheus


def test_a_service_monitor_scrapes_envoys_statistics():
    docs = _docs(
        *ON,
        "--set",
        "metrics.serviceMonitor.enabled=true",
        "--api-versions",
        "monitoring.coreos.com/v1/ServiceMonitor",
    )
    monitors = _of("ServiceMonitor", docs)
    (endpoint,) = monitors["gateway"]["spec"]["endpoints"]
    assert endpoint["port"] == "metrics" and endpoint["path"] == "/stats/prometheus"


def test_the_values_schema_is_closed_for_the_gateway():
    out = _helm(*ON, "--set", "gateway.bogus=1")
    assert out.returncode != 0 and "bogus" in out.stderr


def test_the_grpc_upstream_port_is_its_own_value():
    docs = _docs(*ON, "--set", "gateway.upstream.grpcPort=9000")
    config = next(
        d for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "rel-examlops-gateway-envoy"
    )["data"]["envoy.yaml"]  # fmt: skip
    assert "address: ray-serving, port_value: 9000" in config
    assert "port_value: 8081" not in config
