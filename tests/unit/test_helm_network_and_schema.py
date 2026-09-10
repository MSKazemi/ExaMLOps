"""The chart's values schema, NetworkPolicies and ServiceMonitor (ADR 0129, S6).

A Helm chart is an interface consumed by people who never read its templates, so its failure
modes are quiet ones: a misspelt value that is silently ignored, a network policy that lets the
wrong pod through (or blocks the right one), a scrape target that 404s. Each test renders the real
chart with the pinned helm and asserts one of those outcomes cannot happen. Skipped without helm,
like the other chart guards; the CI `helm chart` job runs this file with helm installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "platform" / "infra" / "helm" / "examlops"
HELM = shutil.which("helm") or shutil.which("helm", path=str(Path.home() / ".local" / "bin"))
REGISTRY = ("--set", "global.imageRegistry=ghcr.io/mskazemi/")
SM_API = ("--api-versions", "monitoring.coreos.com/v1/ServiceMonitor")

pytestmark = pytest.mark.skipif(HELM is None, reason="helm is not installed")


def _helm(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [HELM, "template", "rel", str(CHART), *REGISTRY, *extra], capture_output=True, text=True
    )


def _docs(*extra: str) -> list[dict]:
    out = _helm(*extra)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _policies(*extra: str) -> dict[str, dict]:
    docs = _docs("--set", "networkPolicy.enabled=true", *extra)
    return {
        d["metadata"]["labels"]["app.kubernetes.io/component"]: d
        for d in docs
        if d["kind"] == "NetworkPolicy"
    }


def _peers(rules: list[dict]) -> list[str]:
    """Each allowed peer as `component:<c>` or `namespace:<name>` or `any`."""
    found = []
    for rule in rules:
        peers = rule.get("from", rule.get("to"))
        if not peers:
            found.append("any")
            continue
        for peer in peers:
            if "podSelector" in peer:
                found.append(
                    "component:" + peer["podSelector"]["matchLabels"]["app.kubernetes.io/component"]
                )
            else:
                labels = peer["namespaceSelector"]["matchLabels"]
                found.append("namespace:" + labels["kubernetes.io/metadata.name"])
    return found


# ── values schema ──────────────────────────────────────────────────────────────────────────


def test_defaults_satisfy_the_schema():
    assert _helm().returncode == 0


@pytest.mark.parametrize(
    "override, key",
    [
        ("controlPlane.replicaCont=2", "replicacont"),  # a typo: silently ignored without a schema
        ("dashboard.port=70000", "port"),
        ("controlPlane.image.pullPolicy=Sometimes", "pullpolicy"),
        ("global.imageRegistry=ghcr.io/mskazemi", "imageregistry"),  # no trailing slash
    ],
)
def test_schema_rejects_values_that_would_misbehave(override: str, key: str):
    """Asserts only what every helm 3 JSON-schema backend reports: a failure naming the key."""
    out = _helm("--set", override)
    assert out.returncode != 0, f"--set {override} rendered"
    assert "schema" in out.stderr.lower() and key in out.stderr.lower(), out.stderr


# ── NetworkPolicy ──────────────────────────────────────────────────────────────────────────


def test_network_policies_are_opt_in():
    assert not [d for d in _docs() if d["kind"] == "NetworkPolicy"]


def test_each_tier_is_default_deny_both_ways():
    policies = _policies()
    assert set(policies) == {"control-plane", "dashboard", "agent"}
    for tier, pol in policies.items():
        spec = pol["spec"]
        assert spec["policyTypes"] == ["Ingress", "Egress"], tier
        assert spec["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == tier


def test_ingress_is_exactly_the_documented_flows():
    policies = _policies()
    assert sorted(_peers(policies["control-plane"]["spec"]["ingress"])) == [
        "component:agent",
        "component:dashboard",
        "namespace:ingress-nginx",
        "namespace:monitoring",
    ]
    assert sorted(_peers(policies["dashboard"]["spec"]["ingress"])) == ["namespace:ingress-nginx"]
    assert _peers(policies["agent"]["spec"]["ingress"]) == ["component:dashboard"]


def test_in_cluster_egress_follows_the_callers():
    policies = _policies()
    assert {"component:control-plane", "component:agent"} <= set(
        _peers(policies["dashboard"]["spec"]["egress"])
    )
    assert "component:control-plane" in _peers(policies["agent"]["spec"]["egress"])
    assert not [
        p for p in _peers(policies["control-plane"]["spec"]["egress"]) if p.startswith("component:")
    ]


def test_every_tier_can_resolve_names():
    for tier, pol in _policies().items():
        dns = [r for r in pol["spec"]["egress"] if {p["port"] for p in r.get("ports", [])} == {53}]
        assert dns and not dns[0].get("to"), f"{tier} has no DNS egress"


def test_external_egress_is_port_scoped():
    for tier, pol in _policies().items():
        for rule in pol["spec"]["egress"]:
            assert rule.get("ports"), f"{tier}: an egress rule without ports allows everything"


def test_disabling_the_agent_removes_every_path_to_it():
    policies = _policies("--set", "agent.enabled=false")
    assert set(policies) == {"control-plane", "dashboard"}
    everything = yaml.safe_dump(list(policies.values()))
    assert "component: agent" not in everything


# ── ServiceMonitor ─────────────────────────────────────────────────────────────────────────


def test_service_monitor_is_opt_in_and_refuses_without_the_crd():
    assert not [d for d in _docs(*SM_API) if d["kind"] == "ServiceMonitor"]
    out = _helm("--set", "metrics.serviceMonitor.enabled=true")
    assert out.returncode != 0 and "Prometheus Operator CRDs" in out.stderr


def test_service_monitor_scrapes_the_one_tier_that_serves_metrics():
    docs = _docs(*SM_API, "--set", "metrics.serviceMonitor.enabled=true")
    (sm,) = [d for d in docs if d["kind"] == "ServiceMonitor"]
    selector = sm["spec"]["selector"]["matchLabels"]
    assert selector["app.kubernetes.io/component"] == "control-plane"
    (endpoint,) = sm["spec"]["endpoints"]
    assert endpoint["path"] == "/metrics" and endpoint["port"] == "http"
    services = [d for d in docs if d["kind"] == "Service"]
    target = [s for s in services if s["spec"]["selector"] == selector]
    assert target and any(p["name"] == "http" for p in target[0]["spec"]["ports"]), (
        "the ServiceMonitor selects no Service with an `http` port"
    )


def test_values_without_the_agent_toggle_still_isolate_the_agent():
    """Older values files have no `agent.enabled`; the agent is deployed, so it must be isolated."""
    policies = _policies("--set", "agent.enabled=null")
    assert set(policies) == {"control-plane", "dashboard", "agent"}
