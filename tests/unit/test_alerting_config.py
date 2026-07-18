"""Alerting-pipeline config gate (enterprise-readiness item 3.4 / QW3).

Guards the Alertmanager route tree + Prometheus rules so the delivery pipeline that today
fires 25+ rules into a black hole can never silently regress. Structural (YAML) checks — no
running Alertmanager needed — assert the pieces the deliverable requires: a severity/cluster/
service route tree, PagerDuty + Slack receivers, inhibition, and an always-firing Watchdog
heartbeat wired to a dead-man's-switch receiver.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_DC = Path(__file__).parents[2] / "platform" / "infra" / "docker-compose"


def _load(name: str) -> dict:
    return yaml.safe_load((_DC / name).read_text())


def test_alertmanager_route_tree_branches_by_severity_and_heartbeat():
    am = _load("alertmanager.yml")
    routes = am["route"]["routes"]
    receivers_by_match = {}
    for r in routes:
        matchers = " ".join(r.get("matchers", []))
        receivers_by_match[matchers] = r["receiver"]
    # Heartbeat, critical, and warning routes must all exist.
    assert any("Watchdog" in m for m in receivers_by_match), "no heartbeat route"
    assert any('severity = "critical"' in m for m in receivers_by_match), "no critical route"
    assert any('severity = "warning"' in m for m in receivers_by_match), "no warning route"
    # group_by carries cluster + service so routing/grouping is fleet-aware.
    assert {"cluster", "service"} <= set(am["route"]["group_by"])


def test_alertmanager_receivers_have_pagerduty_and_slack_and_heartbeat():
    am = _load("alertmanager.yml")
    receivers = {r["name"]: r for r in am["receivers"]}
    assert {"null", "heartbeat", "critical", "warning"} <= set(receivers)
    # Critical pages AND chats.
    assert receivers["critical"].get("pagerduty_configs"), "critical must page PagerDuty"
    assert receivers["critical"].get("slack_configs"), "critical must also post to Slack"
    assert receivers["warning"].get("slack_configs"), "warning must post to Slack"
    # Heartbeat delivers to an external dead-man's-switch webhook.
    assert receivers["heartbeat"].get("webhook_configs"), "heartbeat must webhook a snitch"


def test_alertmanager_secrets_are_file_based_not_inlined():
    """No webhook URL / routing key may be committed inline — must be *_file references."""
    raw = (_DC / "alertmanager.yml").read_text()
    assert "hooks.slack.com/services/" not in raw, "inlined Slack webhook — use slack_api_url_file"
    assert "routing_key_file" in raw and "url_file" in raw
    assert "slack_api_url_file" in raw


def test_alertmanager_has_inhibition_rules():
    am = _load("alertmanager.yml")
    inhibits = am.get("inhibit_rules", [])
    assert inhibits, "no inhibition — critical + warning will double-notify"
    # A critical must be able to mute a sibling warning within the same service+cluster.
    crit_mutes_warn = any(
        any("critical" in s for s in ir.get("source_matchers", []))
        and any("warning" in t for t in ir.get("target_matchers", []))
        and {"cluster", "service"} <= set(ir.get("equal", []))
        for ir in inhibits
    )
    assert crit_mutes_warn, "missing critical-inhibits-warning rule keyed on cluster+service"


def test_watchdog_heartbeat_rule_exists_and_always_fires():
    rules = _load("alert_rules.yml")
    watchdog = None
    for group in rules["groups"]:
        for rule in group.get("rules", []):
            if rule.get("alert") == "Watchdog":
                watchdog = rule
    assert watchdog is not None, "no Watchdog rule — nothing proves alert *delivery*"
    assert watchdog["expr"].strip() == "vector(1)", "Watchdog must always fire"
    assert watchdog["labels"]["service"] == "heartbeat"


def test_every_alert_carries_a_service_label():
    """The route tree keys on `service`; a rule without it can't be routed/inhibited precisely."""
    rules = _load("alert_rules.yml")
    missing = [
        rule["alert"]
        for group in rules["groups"]
        for rule in group.get("rules", [])
        if "service" not in rule.get("labels", {})
    ]
    assert not missing, f"alerts missing a service label: {missing}"


def test_prometheus_external_labels_stamp_cluster():
    prom = _load("prometheus.yml")
    ext = prom["global"].get("external_labels", {})
    assert "cluster" in ext, "external_labels must stamp cluster for fleet-aware routing"
