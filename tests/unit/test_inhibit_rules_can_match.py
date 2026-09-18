"""An inhibit rule that can never match is a silencing that does not happen.

Alertmanager's second inhibit rule names four "X is down" alerts as sources that mute the derived
symptom alerts of the same component, and it scopes that with `equal: ['cluster', 'service']`. So
a source only ever mutes alerts carrying the **same `service` label**. `RayServeTargetDown` carried
`service: platform` while every Ray Serve symptom carried `service: serving`, so the rule was inert
for the entire serving plane: when the model server went down, its symptom alerts were never muted.

Nothing catches that by reading either file alone — the alert is valid, the inhibit rule is valid,
and `amtool check-config` is happy. Only holding the two against each other shows it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
D = ROOT / "platform" / "infra" / "docker-compose"
RULES = yaml.safe_load((D / "alert_rules.yml").read_text(encoding="utf-8"))
AM = yaml.safe_load((D / "alertmanager.yml").read_text(encoding="utf-8"))


def _services() -> dict[str, str]:
    return {
        r["alert"]: r.get("labels", {}).get("service", "")
        for g in RULES.get("groups", [])
        for r in g.get("rules", [])
        if "alert" in r
    }


def _source_alertnames() -> list[str]:
    """Alert names an inhibit rule names as a source, from a `alertname =~ "A|B"` matcher."""
    names = []
    for rule in AM.get("inhibit_rules", []):
        for matcher in rule.get("source_matchers", []):
            if matcher.startswith("alertname =~"):
                pattern = matcher.split("=~", 1)[1].strip().strip('"')
                names.extend(n for n in pattern.split("|") if n)
    return names


def test_the_inhibit_rules_name_sources_we_can_check():
    """If this finds nothing the test below passes vacuously."""
    named = _source_alertnames()
    assert len(named) >= 3, named
    assert set(named) <= set(_services()), f"names no such alert: {set(named) - set(_services())}"


def test_every_named_inhibit_source_can_actually_mute_something():
    services = _services()
    inert = []
    for source in _source_alertnames():
        service = services[source]
        others = [a for a, s in services.items() if s == service and a != source]
        if not others:
            inert.append(f"{source} (service={service!r}) is the only alert with that service")
    assert not inert, (
        "these alerts are named as inhibit sources but share their `service` label with no other "
        "alert, and the rule scopes muting with `equal: ['cluster', 'service']` — so they silence "
        f"nothing: {inert}"
    )


def test_the_serving_target_down_alert_can_mute_the_serving_symptoms():
    """The specific case that was wrong: the model server's own symptoms."""
    services = _services()
    assert services["RayServeTargetDown"] == "serving", (
        "RayServeTargetDown must carry the service label of the alerts it is meant to mute; with "
        f"{services['RayServeTargetDown']!r} the inhibit rule never matches them"
    )
    muted = [a for a, s in services.items() if s == "serving" and a != "RayServeTargetDown"]
    assert len(muted) >= 5, muted
