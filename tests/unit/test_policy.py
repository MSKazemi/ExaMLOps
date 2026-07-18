"""Unit tests for the policy-as-code decision point (INC-3 / ADR 0079)."""

from __future__ import annotations

import examlops.policy as policy
from examlops.policy import Decision, decide


def test_default_allow_with_no_policies():
    """No policy file / no rules ⇒ allow (backward compatible)."""
    d = decide("retrain", {"model": "JPCP"}, policies=[], audit=False)
    assert isinstance(d, Decision)
    assert d.allowed and not d.denied and not d.requires_approval
    assert d.rule is None


def test_catch_all_deny_blocks_action():
    rules = [{"action": "retrain", "effect": "deny"}]
    d = decide("retrain", {"model": "JPCP"}, policies=rules, audit=False)
    assert d.denied
    assert d.rule is not None


def test_conditional_allow_else_require_approval():
    rules = [
        {"action": "promote", "when": "rmse_new < rmse_prod and env != 'prod'", "effect": "allow"},
        {"action": "promote", "effect": "require_approval"},
    ]
    good = decide(
        "promote", {"rmse_new": 4.0, "rmse_prod": 5.0, "env": "dev"}, policies=rules, audit=False
    )
    assert good.allowed
    regressed = decide(
        "promote", {"rmse_new": 6.0, "rmse_prod": 5.0, "env": "dev"}, policies=rules, audit=False
    )
    assert regressed.requires_approval
    prod = decide(
        "promote", {"rmse_new": 4.0, "rmse_prod": 5.0, "env": "prod"}, policies=rules, audit=False
    )
    assert prod.requires_approval


def test_first_matching_rule_wins():
    rules = [
        {"action": "retrain", "effect": "allow", "name": "first"},
        {"action": "retrain", "effect": "deny", "name": "second"},
    ]
    d = decide("retrain", {}, policies=rules, audit=False)
    assert d.allowed and d.rule == "first"


def test_wildcard_action_matches_everything():
    rules = [{"action": "*", "effect": "deny"}]
    assert decide("anything", {}, policies=rules, audit=False).denied


def test_bad_condition_does_not_match_and_falls_through():
    """A condition that errors (undefined name) must NOT match — a later catch-all decides."""
    rules = [
        {"action": "promote", "when": "undefined_var > 3", "effect": "allow"},
        {"action": "promote", "effect": "deny"},
    ]
    d = decide("promote", {}, policies=rules, audit=False)
    assert d.denied  # the bad-condition rule was skipped, catch-all deny applied


def test_condition_is_sandboxed_no_code_execution():
    """The condition tier must reject attribute/dunder access (T2 sandbox, ADR 0081)."""
    rules = [{"action": "x", "when": "().__class__.__bases__", "effect": "allow"}]
    # Sandbox rejects the expression → rule doesn't match → default allow (no crash, no exec).
    d = decide("x", {}, policies=rules, audit=False)
    assert d.allowed and d.rule is None


def test_decision_is_audited(monkeypatch):
    """decide(audit=True) writes exactly one audit_events row with the effect + rule."""
    calls = []

    monkeypatch.setattr("examlops.data.audit.write_audit_event", lambda **kw: calls.append(kw))
    rules = [{"action": "retrain", "effect": "deny", "name": "block"}]
    decide("retrain", {"model": "JPCP"}, policies=rules, audit=True)
    assert len(calls) == 1
    assert calls[0]["action"] == "policy:retrain"
    assert calls[0]["details"]["effect"] == "deny"
    assert calls[0]["details"]["rule"] == "block"
    assert calls[0]["target"] == "JPCP"


def test_audit_failure_never_raises(monkeypatch):
    def boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("examlops.data.audit.write_audit_event", boom)
    # Must still return a decision, not raise.
    d = decide("retrain", {}, policies=[{"action": "retrain", "effect": "allow"}], audit=True)
    assert d.allowed


def test_load_policies_missing_file(tmp_path):
    assert policy._load_policies(tmp_path / "nope.yaml") == []
