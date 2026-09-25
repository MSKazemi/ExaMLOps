"""ADR 0081 rule 3 + ADR 0082 layer 4 — every agent-callable write is dry-run-able and confirm-gated,
and a high-impact change applied by an agent needs a human approval token.

Outcome-asserting: every refusal is checked against the state it was meant to protect.
"""

from __future__ import annotations

import inspect
import typing

import pytest

from examlops import plans
from examlops.mcp import tools, write_safety
from examlops.mcp.tools import REGISTRY
from tests.unit.test_mcp_agent_write_policy import WRITE_ARGS

SPLIT = {"model": "JPCP", "production": 90, "canary": 10}
RULE = {"model": "JPCP", "metric": "rmse", "operator": "<", "threshold": 5.0}
GRANT = {"subject": "guard-user", "relation": "viewer", "obj": "project:guard-proj"}


def _spec(name):
    return next(s for s in REGISTRY if s.name == name)


def _split():
    return tools.traffic_rules("JPCP")["rules"]


def _rule():
    return tools.promotion_rule("JPCP")["rule"]


def _actions():
    return [e["action"] for e in tools.recent_audit_events(limit=100)["events"]]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    for var in (
        "EXAMLOPS_PRINCIPAL_KIND",
        "EXAMLOPS_MCP_AUTO_CONFIRM",
        "EXAMLOPS_MCP_CONFIRM_TIERS",
        "EXAMLOPS_MCP_HITL_TIERS",
        "EXAMLOPS_PLAN_TTL",
    ):
        monkeypatch.delenv(var, raising=False)
    import examlops.policy as policy

    monkeypatch.setattr(policy, "_load_policies", lambda path=None: [])


@pytest.fixture()
def agent(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")


MUTATING = [s for s in REGISTRY if s.mutating and s.name not in plans.PLAN_TOOLS]


# ── schema ────────────────────────────────────────────────────────────────────


def test_the_guard_walks_a_real_registry():
    assert len(MUTATING) >= 10


@pytest.mark.parametrize("spec", MUTATING, ids=lambda s: s.name)
def test_every_mutating_tool_exposes_dry_run_and_confirm(spec):
    params = inspect.signature(spec.fn).parameters
    for name in ("dry_run", "confirm"):
        assert name in params, spec.name
        assert params[name].default is False
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
    hints = typing.get_type_hints(spec.fn)  # LangChain/pydantic introspection path
    assert hints["dry_run"] is bool and hints["confirm"] is bool
    assert "idempotency_key" in params  # the idempotency wrapper is still reachable


def test_read_and_plan_tools_do_not_grow_control_parameters():
    for spec in REGISTRY:
        if not spec.mutating or spec.name in plans.PLAN_TOOLS:
            assert "dry_run" not in inspect.signature(spec.fn).parameters, spec.name


@pytest.mark.parametrize("spec", MUTATING, ids=lambda s: s.name)
def test_every_mutating_tool_dry_runs_without_changing_anything(spec, dead_services):
    before = tools.recent_audit_events(limit=100)["events"]
    out = spec.fn(**WRITE_ARGS[spec.name], dry_run=True)
    assert out["ok"] is True and out["dry_run"] is True and out["changed"] is False, out
    preview = out["preview"]
    assert preview["tool"] == spec.name
    assert preview["intended_change"]
    assert preview["blast_radius"]["tier"] == spec.tier
    assert preview["policy"]["effect"] == "allow"
    after = tools.recent_audit_events(limit=100)["events"]
    # No write audit (the tool body never ran); only policy bookkeeping may appear.
    written = {e["action"] for e in after} - {e["action"] for e in before}
    assert not written & {
        "traffic_split_set",
        "promotion_rule_set",
        "drift_autoretrain_set",
        "challenger_disabled",
        "access_granted",
        "cluster_approved",
        "project_member_added",
        "project_model_assigned",
        "retrain_triggered",
    }, written


# ── dry run ───────────────────────────────────────────────────────────────────


def test_dry_run_reports_current_state_and_does_not_mutate():
    _spec("set_traffic_split").fn(model="JPCP", production=70, canary=30)
    out = _spec("set_traffic_split").fn(**SPLIT, dry_run=True)
    assert out["preview"]["current_state"]["state"]["production"] == 70
    assert out["preview"]["would_succeed"] is True
    assert _split()["production"] == 70


def test_dry_run_is_allowed_to_an_agent_without_a_plan(agent):
    out = _spec("set_promotion_rule").fn(**RULE, dry_run=True)
    assert out["ok"] and out["preview"]["principal_kind"] == "agent"
    assert out["preview"]["required_approvals"] == ["human_approval"]  # tier B → HITL
    assert out["preview"]["needs_confirmation"] is False  # agents cannot confirm; they plan
    assert _rule() is None
    # The same call without dry_run is still refused to an agent.
    assert _spec("set_promotion_rule").fn(**RULE)["code"] == "plan_required"


def test_dry_run_of_a_tier_c_tool_tells_an_agent_it_may_not_apply(agent):
    out = _spec("grant_access").fn(**GRANT, dry_run=True)
    assert out["preview"]["agent_may_apply"] is False


def test_dry_run_reports_a_policy_denial_instead_of_failing(monkeypatch):
    import examlops.policy as policy

    monkeypatch.setattr(
        policy, "_load_policies", lambda path=None: [{"action": "agent_write", "effect": "deny"}]
    )
    out = _spec("set_traffic_split").fn(**SPLIT, dry_run=True)
    assert out["ok"] is True
    assert out["preview"]["policy"]["effect"] == "deny"
    assert out["preview"]["would_succeed"] is False


def test_dry_run_with_bad_arguments_is_a_clean_error():
    out = _spec("set_traffic_split").fn(model="JPCP", nope=1, dry_run=True)
    assert out["ok"] is False and out["code"] == "invalid_args"


# ── confirmation (human principal, direct call) ──────────────────────────────


def test_tier_b_without_confirm_is_refused_with_a_preview_and_nothing_changes():
    out = _spec("set_promotion_rule").fn(**RULE)
    assert out["ok"] is False and out["code"] == "confirmation_required"
    assert out["tier"] == "B"
    assert out["preview"]["intended_change"].startswith("set promotion rule for JPCP")
    assert _rule() is None
    assert "promotion_rule_set" not in _actions()


def test_tier_b_with_confirm_is_applied_and_audited():
    out = _spec("set_promotion_rule").fn(**RULE, confirm=True)
    assert out["ok"] is True, out
    assert _rule() is not None
    assert "promotion_rule_set" in _actions()


def test_auto_confirm_is_the_explicit_yes(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MCP_AUTO_CONFIRM", "1")
    assert _spec("set_promotion_rule").fn(**RULE)["ok"] is True
    assert _rule() is not None


def test_ci_alone_does_not_auto_confirm(monkeypatch):
    monkeypatch.setenv("CI", "true")
    assert _spec("set_promotion_rule").fn(**RULE)["code"] == "confirmation_required"
    assert _rule() is None


def test_host_side_confirmation_context_satisfies_the_gate():
    with write_safety.confirmed():
        assert _spec("set_promotion_rule").fn(**RULE)["ok"] is True
    # ...and does not leak past the context.
    assert _spec("set_promotion_rule").fn(**{**RULE, "threshold": 4.0})["code"] == (
        "confirmation_required"
    )


def test_tier_a_is_not_confirm_gated_by_default():
    assert _spec("set_traffic_split").fn(**SPLIT)["ok"] is True
    assert _split()["production"] == 90


def test_confirm_tiers_are_configurable(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MCP_CONFIRM_TIERS", "A,B,C")
    assert _spec("set_traffic_split").fn(**SPLIT)["code"] == "confirmation_required"
    assert _split() is None


@pytest.mark.parametrize("raw", ["bogus", "B,Z", ",", "  "])
def test_a_malformed_confirm_tier_list_keeps_the_safe_default(monkeypatch, raw):
    monkeypatch.setenv("EXAMLOPS_MCP_CONFIRM_TIERS", raw)
    assert write_safety.confirm_tiers() == frozenset({"B", "C"})
    assert _spec("set_promotion_rule").fn(**RULE)["code"] == "confirmation_required"


def test_confirm_tiers_none_switches_confirmation_off_only_when_explicit(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MCP_CONFIRM_TIERS", "none")
    assert _spec("set_promotion_rule").fn(**RULE)["ok"] is True


def test_a_policy_denial_is_reported_before_asking_for_confirmation(monkeypatch):
    import examlops.policy as policy

    monkeypatch.setattr(
        policy, "_load_policies", lambda path=None: [{"action": "agent_write", "effect": "deny"}]
    )
    out = _spec("set_promotion_rule").fn(**RULE)
    assert out["code"] == "policy_denied" and "policy denied" in out["error"]
    assert _rule() is None


def test_an_unreadable_preview_still_refuses(monkeypatch):
    def boom(tool, args):
        raise RuntimeError("state store down")

    monkeypatch.setattr(plans, "_snapshot", boom)
    out = _spec("set_promotion_rule").fn(**RULE)
    assert out["code"] == "confirmation_required"
    assert out["preview"]["current_state_error"] == "state store down"
    assert _rule() is None


def test_an_agent_setting_confirm_does_not_bypass_the_plan_gate(agent):
    out = _spec("set_promotion_rule").fn(**RULE, confirm=True)
    assert out["code"] == "plan_required"
    assert _rule() is None


# ── plan/apply: control args are never part of a plan ───────────────────────


def test_control_arguments_do_not_change_the_plan():
    a = tools.plan_change("set_traffic_split", SPLIT)["plan"]
    b = tools.plan_change("set_traffic_split", {**SPLIT, "confirm": True, "dry_run": False})["plan"]
    assert a["plan_hash"] == b["plan_hash"]
    assert "confirm" not in b["args"] and "dry_run" not in b["args"]


def test_apply_runs_the_tool_without_a_second_confirmation():
    plan = tools.plan_change("set_promotion_rule", RULE)["plan"]
    assert plan["required_approvals"] == []  # a human planned it; the human applies it
    out = tools.apply_plan(plan["plan_hash"])
    assert out["ok"] is True, out
    assert _rule() is not None


# ── ADR 0082 layer 4: HITL for high-impact agent writes ──────────────────────


def test_agent_tier_b_plan_needs_a_human_approval_token(agent, monkeypatch):
    plan = tools.plan_change("set_promotion_rule", RULE)["plan"]
    assert plan["required_approvals"] == ["human_approval"]
    h = plan["plan_hash"]
    assert tools.apply_plan(h)["code"] == "approval_required"
    assert _rule() is None
    assert tools.approve_plan(h)["code"] == "approval_requires_human"
    monkeypatch.delenv("EXAMLOPS_PRINCIPAL_KIND")
    token = tools.approve_plan(h)["approval_token"]
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")
    out = tools.apply_plan(h, token)
    assert out["ok"] is True, out
    assert _rule() is not None
    assert {"plan_approved", "plan_applied", "promotion_rule_set"} <= set(_actions())


def test_human_planned_tier_b_still_needs_approval_when_an_agent_applies(monkeypatch):
    plan = tools.plan_change("set_promotion_rule", RULE)["plan"]
    assert plan["required_approvals"] == []
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")
    assert tools.apply_plan(plan["plan_hash"])["code"] == "approval_required"
    assert _rule() is None


def test_cluster_approval_by_an_agent_is_human_gated(agent):
    """ADR 0082 names cluster approval as a layer-4 action: it is tier B, so it needs a human."""
    assert _spec("hpc_approve_cluster").tier == "B"
    out = _spec("hpc_approve_cluster").fn(name="guard-cluster", dry_run=True)
    assert out["preview"]["required_approvals"] == ["human_approval"]


def test_agent_tier_a_plan_needs_no_approval(agent):
    plan = tools.plan_change("set_traffic_split", SPLIT)["plan"]
    assert plan["required_approvals"] == []
    assert tools.apply_plan(plan["plan_hash"])["ok"] is True
    assert _split()["production"] == 90


def test_hitl_tiers_none_lets_an_agent_apply_tier_b(agent, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MCP_HITL_TIERS", "none")
    plan = tools.plan_change("set_promotion_rule", RULE)["plan"]
    assert plan["required_approvals"] == []
    assert tools.apply_plan(plan["plan_hash"])["ok"] is True


@pytest.mark.parametrize("raw", ["garbage", "B;C", "X"])
def test_a_malformed_hitl_tier_list_keeps_the_human_gate(agent, monkeypatch, raw):
    monkeypatch.setenv("EXAMLOPS_MCP_HITL_TIERS", raw)
    assert plans.hitl_tiers() == frozenset({"B", "C"})
    plan = tools.plan_change("set_promotion_rule", RULE)["plan"]
    assert tools.apply_plan(plan["plan_hash"])["code"] == "approval_required"


def test_an_agent_can_never_apply_a_tier_c_plan_a_human_made(monkeypatch):
    plan = tools.plan_change("grant_access", GRANT)["plan"]
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")
    out = tools.apply_plan(plan["plan_hash"])
    assert out["code"] == "tier_c_human_only"
    assert not tools.authz_relations("guard-user", "project:guard-proj").get("relations")
    assert "plan_apply_refused" in _actions()
