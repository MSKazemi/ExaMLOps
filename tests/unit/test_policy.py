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

    # Positional, because `_audit` now goes through `audit_best_effort`, which forwards
    # positionally. A `**kw`-only fake raised TypeError here, the helper counted it as a lost
    # audit event, and the list stayed empty — a fake that cannot be called is indistinguishable
    # from an audit that was never attempted.
    def record(source, actor, action, target, details=None, **kw):
        calls.append(
            {
                "source": source,
                "actor": actor,
                "action": action,
                "target": target,
                "details": details,
                **kw,
            }
        )

    monkeypatch.setattr("examlops.data.audit.write_audit_event", record)
    rules = [{"action": "retrain", "effect": "deny", "name": "block"}]
    decide("retrain", {"model": "JPCP"}, policies=rules, audit=True)
    assert len(calls) == 1
    assert calls[0]["action"] == "policy:retrain"
    assert calls[0]["details"]["effect"] == "deny"
    assert calls[0]["details"]["rule"] == "block"
    assert calls[0]["target"] == "JPCP"


def test_audit_failure_never_raises(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("examlops.data.audit.write_audit_event", boom)
    # Must still return a decision, not raise.
    d = decide("retrain", {}, policies=[{"action": "retrain", "effect": "allow"}], audit=True)
    assert d.allowed


# ── decide_safe: a raising engine is not one whose "allow" can be trusted (BL-080) ──────────────


def test_decide_safe_passes_through_a_normal_decision_unchanged():
    """The common path: the engine doesn't raise, so `decide_safe` is just `decide`."""
    rules = [{"action": "retrain", "effect": "deny", "name": "block"}]
    d = policy.decide_safe("retrain", {"model": "JPCP"}, policies=rules, audit=False)
    assert d.denied and not d.unavailable
    assert d.rule == "block"


def test_decide_safe_defaults_to_deny_when_the_engine_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("engine broke")

    monkeypatch.setattr(policy, "decide", boom)
    d = policy.decide_safe("retrain", {"model": "JPCP"})
    assert d.denied
    assert d.unavailable
    assert "engine broke" in d.reason


def test_decide_safe_honours_a_caller_chosen_default(monkeypatch):
    """Not every caller wants deny — a caller may document a specific reason to pick otherwise."""

    def boom(*a, **k):
        raise RuntimeError("engine broke")

    monkeypatch.setattr(policy, "decide", boom)
    d = policy.decide_safe("retrain", {}, default_effect=policy.ALLOW)
    assert d.allowed and d.unavailable


def test_decide_safe_durably_audits_the_unavailable_decision(monkeypatch):
    """Unlike a bare `except: log.warning(...)`, this must leave a row in `audit_events` — the
    gap BL-080 found: the old fallback was invisible to any review of the audit chain."""
    calls = []

    def record(source, actor, action, target, details=None, **kw):
        calls.append({"source": source, "action": action, "target": target, "details": details})

    monkeypatch.setattr("examlops.data.audit.write_audit_event", record)

    def boom(*a, **k):
        raise RuntimeError("engine broke")

    monkeypatch.setattr(policy, "decide", boom)
    policy.decide_safe("retrain", {"model": "JPCP"})

    assert len(calls) == 1
    assert calls[0]["action"] == "policy_unavailable:retrain"
    assert calls[0]["target"] == "JPCP"
    assert calls[0]["details"]["error"] == "engine broke"
    assert calls[0]["details"]["default_effect"] == "deny"


def test_decide_safe_survives_a_broken_audit_path_too(monkeypatch):
    """A broken policy engine AND a broken audit path must still return a decision, not raise.
    `audit_best_effort` already swallows a broken `write_audit_event` on its own (this is really
    an integration check that the two failures compose safely), but `decide_safe` carries its own
    belt-and-braces `except` around the audit call too, matching this codebase's habit of never
    trusting a single layer to be the only thing standing between a bug and a crash."""

    def boom(*a, **k):
        raise RuntimeError("engine broke")

    def audit_boom(*a, **k):
        raise RuntimeError("audit down too")

    monkeypatch.setattr(policy, "decide", boom)
    monkeypatch.setattr("examlops.data.audit.write_audit_event", audit_boom)
    d = policy.decide_safe("retrain", {"model": "JPCP"})
    assert d.denied and d.unavailable


def test_load_policies_missing_file(tmp_path):
    assert policy._load_policies(tmp_path / "nope.yaml") == []


# ── an unreadable policy file is not the same state as no policy file ─────────
#
# The layer defaults to `allow`, so a policy.yaml that does not parse silently removes every gate
# the operator wrote — including a human-approval gate on an autopilot promote. Fail-open is
# deliberate (a broken file must not wedge a mutation path) but it must not be silent, and
# `exa policy list` used to report a file that was right there as "absent".

_GOOD = """policies:
  - action: autopilot_promote
    effect: require_approval
"""
_BROKEN = 'policies:\n  - action: autopilot_promote\n    effect: "unterminated\n'


def test_a_valid_file_loads_with_no_error(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text(_GOOD)
    rules, error = policy.load_policies_with_status(f)
    assert error is None
    assert rules == [{"action": "autopilot_promote", "effect": "require_approval"}]


def test_a_missing_file_is_not_an_error(tmp_path):
    rules, error = policy.load_policies_with_status(tmp_path / "nope.yaml")
    assert (rules, error) == ([], None), "no file is a legitimate 'no policies'"


def test_an_empty_file_is_not_an_error(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text("   \n")
    assert policy.load_policies_with_status(f) == ([], None)


def test_an_unparsable_file_reports_why(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text(_BROKEN)
    rules, error = policy.load_policies_with_status(f)
    assert rules == []
    assert error and str(f) in error, "must name the file the operator has to fix"
    assert "could not be parsed" in error


def test_a_file_with_the_wrong_key_reports_why(tmp_path):
    # `rules:` instead of `policies:` — parses fine, gates nothing.
    f = tmp_path / "policy.yaml"
    f.write_text("rules:\n  - action: promote\n    effect: deny\n")
    rules, error = policy.load_policies_with_status(f)
    assert rules == []
    assert error and "policies" in error


def test_a_broken_file_still_fails_open_but_warns(tmp_path, caplog):
    # The mutation path must not wedge; it must also not go quiet.
    f = tmp_path / "policy.yaml"
    f.write_text(_BROKEN)
    with caplog.at_level("WARNING", logger="examlops.policy"):
        assert policy._load_policies(f) == []
    assert any("falls back to 'allow'" in r.getMessage() for r in caplog.records)


def test_the_gate_really_does_disappear_when_the_file_breaks(tmp_path, monkeypatch):
    # The reason any of this matters, stated as a test.
    good = tmp_path / "good.yaml"
    good.write_text(_GOOD)
    assert decide(
        "autopilot_promote", policies=policy._load_policies(good), audit=False
    ).requires_approval

    broken = tmp_path / "broken.yaml"
    broken.write_text(_BROKEN)
    assert decide(
        "autopilot_promote", policies=policy._load_policies(broken), audit=False
    ).allowed, "documented behaviour: fail-open — which is exactly why it must be loud"


def test_unknown_effect_fails_closed():
    """C8: a typo'd effect ('block', 'Denied', …) must deny — never silently allow."""
    rules = [{"action": "promote", "effect": "block", "name": "typo"}]
    d = decide("promote", {}, policies=rules, audit=False)
    assert d.denied and not d.allowed and not d.requires_approval


def test_effect_is_case_insensitive():
    """C8: 'effect: Deny' is deny, not an unknown string that behaves as allow."""
    assert decide("p", {}, policies=[{"action": "p", "effect": "Deny"}], audit=False).denied
    assert decide("p", {}, policies=[{"action": "p", "effect": "ALLOW"}], audit=False).allowed
    d = decide("p", {}, policies=[{"action": "p", "effect": "Require_Approval"}], audit=False)
    assert d.requires_approval


def test_unknown_effect_normalized_to_deny_at_load(tmp_path, caplog):
    """C8: rule-load normalizes/validates effects, warning with the file and rule name."""
    import logging

    p = tmp_path / "policy.yaml"
    p.write_text("policies:\n  - action: promote\n    effect: Blocked\n    name: bad-rule\n")
    with caplog.at_level(logging.WARNING, logger="examlops.policy"):
        rules, err = policy.load_policies_with_status(p)
    assert err is None
    assert rules[0]["effect"] == "deny"  # fail closed, normalized in place
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "Blocked" in warned and "bad-rule" in warned and str(p) in warned
