"""The promtool rule tests stay attached to the rules they test, and run.

``platform/infra/docker-compose/alert_rules_test.yml`` replays input series through the alert
rules (``promtool test rules``; ``make alerts-check`` and CI's ``alert rules`` job). A case that
expects *no* alert passes just as well when the alert has been renamed or removed, so a rename
would turn it into a test of nothing. This holds every case to an alert that exists, and holds
the Makefile and CI to running the file.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
D = ROOT / "platform" / "infra" / "docker-compose"
TESTS = yaml.safe_load((D / "alert_rules_test.yml").read_text(encoding="utf-8"))
RULES = yaml.safe_load((D / "alert_rules.yml").read_text(encoding="utf-8"))
ALERTS = {r["alert"] for g in RULES["groups"] for r in g["rules"] if "alert" in r}


def _cases() -> list[tuple[str, dict]]:
    return [(t["name"], c) for t in TESTS["tests"] for c in t.get("alert_rule_test", [])]


def test_every_case_names_an_alert_that_exists():
    assert _cases(), "no rule tests found"
    unknown = sorted({c["alertname"] for _, c in _cases()} - ALERTS)
    assert not unknown, f"rule tests for alerts that are not in alert_rules.yml: {unknown}"


def test_the_tests_load_the_rules_file_they_sit_next_to():
    assert TESTS["rule_files"] == ["alert_rules.yml"]


def test_each_quiet_case_has_a_firing_case_for_the_same_alert():
    """A case expecting silence proves little unless the same alert is shown to fire somewhere."""
    firing = {c["alertname"] for _, c in _cases() if c.get("exp_alerts")}
    quiet_only = sorted({c["alertname"] for _, c in _cases()} - firing)
    assert not quiet_only, f"alerts only ever tested for silence: {quiet_only}"


def test_make_and_ci_run_the_tests():
    assert "test rules alert_rules_test.yml" in (ROOT / "Makefile").read_text()
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = " ".join(str(s.get("run", "")) for s in ci["jobs"]["alert-rules"]["steps"])
    assert "test rules alert_rules_test.yml" in steps and "check rules alert_rules.yml" in steps
    assert "alert-rules" in ci["jobs"]["ci-ok"]["needs"]


# ── the ratchet: an alert nobody has shown to fire ─────────────────────────────────────────────
#
# `promtool check rules` proves a rule parses, and a separate guard proves its metric is emitted.
# Neither shows that the expression produces an alert for the condition it describes. On
# 2026-09-14 only 13 of 60 alerts had ever been demonstrated firing — the gap was found by
# shipping `AuditEventsDropped` the day before and noticing nothing had made it fire.
#
# The number may only go DOWN, and it has reached **zero**: every alert in the platform has a case
# that makes it fire. It stays a ratchet rather than a bare `== 0` assertion so the failure message
# names what slipped, and so a deliberate exception would have to be written down as a number.
#
#   60 alerts, 11 proven — 2026-09-14, when this ratchet was added
#   49 -> 47   AuditEventsDropped, RetrainDispatchedButNotRecorded
#   47 -> 43   the approval gauges + HighRetrainErrorRate
#   43 -> 41   the two SLO burn-rate alerts — writing the case is what caught the value being the
#              error ratio rather than the burn rate it was labelled as
#   41 -> 38   the Dataplane bus bridge
#   38 -> 37   ServingSnapshotLagging — a replica that had applied NO snapshot published no series,
#              so `min()` was empty and the alert could not fire for the most-behind replica
#   37 -> 32   the event backbone
#   32 -> 28   the dataplane
#   28 -> 23   Ray Serve — writing RayServeTargetDown's case is what found its `service` label made
#              a whole Alertmanager inhibit rule inert
#   23 -> 19   vLLM
#   19 -> 12   the "X is down" family and the Watchdog
#   12 ->  0   the rest of the control plane, the Envoy gateway statistics, the bridge latency
UNPROVEN_ALERT_CEILING = 0


def _never_proven_to_fire() -> list[str]:
    firing = {c["alertname"] for _, c in _cases() if c.get("exp_alerts")}
    return sorted(ALERTS - firing)


def test_no_new_alert_arrives_without_a_case_that_makes_it_fire():
    unproven = _never_proven_to_fire()
    assert len(unproven) <= UNPROVEN_ALERT_CEILING, (
        f"{len(unproven)} alerts have never been shown to fire, ceiling is "
        f"{UNPROVEN_ALERT_CEILING}. A new alert needs a case in alert_rules_test.yml with "
        f"exp_alerts, proving it fires on the input it is written for. Unproven: {unproven}"
    )


def test_the_ceiling_is_not_slack():
    """A ceiling above the real count would let the next alert in unproven, silently."""
    assert len(_never_proven_to_fire()) == UNPROVEN_ALERT_CEILING, (
        f"the ceiling has drifted from reality — lower it to {len(_never_proven_to_fire())}"
    )
