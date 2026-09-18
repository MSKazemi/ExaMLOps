"""The chart's alert rules are Compose's alert rules (runbooks included), for the tiers it deploys.

`metrics.prometheusRule.enabled` renders a PrometheusRule from `files/alert_rules.yml`, a
byte-identical copy of `platform/infra/docker-compose/alert_rules.yml`, which promtool checks and
tests in CI. The chart picks the groups that fit what it deploys: the control plane always, the
serving gateway when it is enabled, and any other group a site names (for a model server or
bridge it scrapes itself).

The rules select jobs by the names Compose's Prometheus gives them (`control_plane`, `gateway`,
`gateway_authz`). With the Prometheus Operator a job would otherwise be the Service's name, and
every `up{job="control_plane"}` rule would silently never fire. So each Service carries
`examlops.io/job`, each ServiceMonitor names it as its `jobLabel`, and a test holds every job a
rendered rule selects to a job a rendered ServiceMonitor produces.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "platform" / "infra" / "helm" / "examlops"
COMPOSE_RULES = ROOT / "platform" / "infra" / "docker-compose" / "alert_rules.yml"
HELM = shutil.which("helm") or shutil.which("helm", path=str(Path.home() / ".local" / "bin"))
BASE = (
    "--set", "global.imageRegistry=ghcr.io/mskazemi/",
    "--api-versions", "monitoring.coreos.com/v1/PrometheusRule",
    "--api-versions", "monitoring.coreos.com/v1/ServiceMonitor",
)  # fmt: skip
RULES = ("--set", "metrics.prometheusRule.enabled=true")
MONITORS = ("--set", "metrics.serviceMonitor.enabled=true")
GATEWAY = ("--set", "gateway.enabled=true")

pytestmark = pytest.mark.skipif(HELM is None, reason="helm is not installed")


def _helm(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [HELM, "template", "rel", str(CHART), "--namespace", "mlops", *BASE, *extra],
        capture_output=True,
        text=True,
    )


def _docs(*extra: str) -> list[dict]:
    out = _helm(*extra)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _source_groups() -> dict[str, dict]:
    return {g["name"]: g for g in yaml.safe_load(COMPOSE_RULES.read_text())["groups"]}


def _rule(docs: list[dict]) -> dict:
    (rule,) = [d for d in docs if d["kind"] == "PrometheusRule"]
    return rule


def test_the_chart_ships_composes_rules_byte_for_byte():
    assert (CHART / "files" / "alert_rules.yml").read_bytes() == COMPOSE_RULES.read_bytes(), (
        "the chart's alert rules differ from Compose's. After changing alert_rules.yml, copy it: "
        "cp platform/infra/docker-compose/alert_rules.yml platform/infra/helm/examlops/files/"
    )


def test_off_by_default():
    assert not [d for d in _docs() if d["kind"] == "PrometheusRule"]


def test_the_crd_must_exist():
    out = subprocess.run(
        [HELM, "template", "rel", str(CHART), "--set", "global.imageRegistry=x/", *RULES],
        capture_output=True,
        text=True,
    )
    assert out.returncode != 0 and "PrometheusRule" in out.stderr


def test_the_control_plane_group_is_rendered_verbatim():
    spec = _rule(_docs(*RULES))["spec"]
    assert [g["name"] for g in spec["groups"]] == ["examlops-control-plane"]
    assert spec["groups"][0] == _source_groups()["examlops-control-plane"]
    assert all(r["annotations"].get("runbook_url") for r in spec["groups"][0]["rules"])


def test_the_gateway_group_comes_with_the_gateway_and_sites_add_their_own():
    names = [g["name"] for g in _rule(_docs(*RULES, *GATEWAY))["spec"]["groups"]]
    assert names == ["examlops-control-plane", "examlops-gateway"]
    names = [
        g["name"]
        for g in _rule(
            _docs(*RULES, "--set", "metrics.prometheusRule.extraGroups={examlops-serving}")
        )["spec"]["groups"]
    ]
    assert names == ["examlops-control-plane", "examlops-serving"]


def test_an_unknown_group_fails_the_render():
    out = _helm(*RULES, "--set", "metrics.prometheusRule.extraGroups={examlops-nonsense}")
    assert out.returncode != 0 and "examlops-nonsense" in out.stderr


def test_labels_reach_the_rule_for_the_operators_selector():
    rule = _rule(_docs(*RULES, "--set", "metrics.prometheusRule.labels.release=kps"))
    assert rule["metadata"]["labels"]["release"] == "kps"


def test_every_job_a_rule_selects_is_a_job_a_service_monitor_produces():
    """Otherwise `up{job="control_plane"} == 0` never matches anything and never fires."""
    docs = _docs(*RULES, *MONITORS, *GATEWAY)
    services = {
        d["metadata"]["name"]: d["metadata"]["labels"] for d in docs if d["kind"] == "Service"
    }
    produced = set()
    for monitor in (d for d in docs if d["kind"] == "ServiceMonitor"):
        assert monitor["spec"]["jobLabel"] == "examlops.io/job"
        selector = monitor["spec"]["selector"]["matchLabels"]
        for labels in services.values():
            if all(labels.get(k) == v for k, v in selector.items()):
                produced.add(labels["examlops.io/job"])
    selected = set()
    for group in _rule(docs)["spec"]["groups"]:
        for rule in group["rules"]:
            for match in re.findall(r'job=~?"([^"]+)"', rule["expr"]):
                selected |= set(match.split("|"))
    assert selected == {"control_plane", "gateway", "gateway_authz"}
    assert selected <= produced, selected - produced


def test_the_gateway_authorization_tier_is_scraped_for_its_decisions():
    docs = _docs(*MONITORS, *GATEWAY)
    monitors = {d["metadata"]["name"]: d for d in docs if d["kind"] == "ServiceMonitor"}
    (endpoint,) = monitors["rel-examlops-gateway-authz"]["spec"]["endpoints"]
    assert endpoint["port"] == "http" and endpoint["path"] == "/metrics"
