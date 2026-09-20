"""ADR 0147 d2 — plan/apply for agent principals (`examlops.plans`)."""

from __future__ import annotations

import threading
import time

import pytest

from examlops import plans
from examlops.data import get_db
from examlops.data import plans as store
from examlops.mcp import tools
from examlops.mcp.tools import REGISTRY


def _spec(name):
    return next(s for s in REGISTRY if s.name == name)


SPLIT = {"model": "JPCP", "production": 90, "canary": 10}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    monkeypatch.delenv("EXAMLOPS_PRINCIPAL_KIND", raising=False)
    monkeypatch.delenv("EXAMLOPS_PLAN_TTL", raising=False)
    import examlops.policy as policy

    monkeypatch.setattr(policy, "_load_policies", lambda path=None: [])


@pytest.fixture()
def agent(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")


def _rules():
    return tools.traffic_rules("JPCP")["rules"]


def _plan(args=None, tool="set_traffic_split"):
    out = tools.plan_change(tool, args or SPLIT)
    assert out["ok"], out
    return out["plan"]


def _actions():
    return [e["action"] for e in tools.recent_audit_events(limit=50)["events"]]


def test_plan_does_not_mutate_and_has_the_documented_shape():
    plan = _plan()
    assert _rules() is None
    for key in ("plan_hash", "tool", "args", "intended_change", "preconditions", "blast_radius"):
        assert key in plan
    assert plan["required_approvals"] == [] and plan["state"] == "planned"
    assert plan["expires_at"] - plan["created_at"] == pytest.approx(900, abs=5)
    assert "plan_created" in _actions()


def test_plan_ttl_env(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_PLAN_TTL", "60")
    plan = _plan()
    assert plan["expires_at"] - plan["created_at"] == pytest.approx(60, abs=5)


def test_plan_then_apply_happy_path(agent):
    plan = _plan()
    out = tools.apply_plan(plan["plan_hash"])
    assert out["ok"] and out["state"] == "applied"
    assert _rules()["production"] == 90
    assert {"plan_applied", "traffic_split_set"} <= set(_actions())
    assert tools.plan_change  # the read side is intact
    assert plans.get_plan(plan["plan_hash"])["plan"]["state"] == "applied"


def test_agent_without_a_plan_is_refused_and_nothing_changes(agent):
    out = _spec("set_traffic_split").fn(**SPLIT)
    assert out["ok"] is False and out["code"] == "plan_required"
    assert _rules() is None


def test_human_principal_is_unaffected():
    out = _spec("set_traffic_split").fn(**SPLIT)
    assert out["ok"] is True and _rules()["production"] == 90


def test_reads_are_never_plan_gated(agent):
    assert tools.traffic_rules("JPCP")["ok"]


@pytest.mark.parametrize("bad", ["", "deadbeef", "0" * 64])
def test_unknown_hash_is_refused(bad):
    out = tools.apply_plan(bad)
    assert out["ok"] is False and out["code"] == "plan_not_found"


def test_a_consumed_plan_cannot_be_applied_again():
    plan = _plan()
    assert tools.apply_plan(plan["plan_hash"])["ok"]
    again = tools.apply_plan(plan["plan_hash"])
    assert again["ok"] is False and again["code"] == "plan_not_applicable"


def test_expired_plan_is_refused_and_marked():
    plan = _plan()
    with get_db() as conn:
        conn.execute(
            "UPDATE agent_plans SET expires_at=? WHERE plan_hash=?",
            (time.time() - 1, plan["plan_hash"]),
        )
    out = tools.apply_plan(plan["plan_hash"])
    assert out["ok"] is False and out["code"] == "plan_expired"
    assert _rules() is None
    assert plans.get_plan(plan["plan_hash"])["plan"]["state"] == "expired"


def test_changed_precondition_is_refused_and_nothing_is_applied():
    plan = _plan()
    # Someone else changes the split after the plan was made.
    _spec("set_traffic_split").fn(model="JPCP", production=50, canary=50)
    out = tools.apply_plan(plan["plan_hash"])
    assert out["ok"] is False and out["code"] == "precondition_changed"
    assert _rules()["production"] == 50
    assert plans.get_plan(plan["plan_hash"])["plan"]["state"] == "rejected"


def test_plan_hash_covers_tool_args_and_preconditions():
    a = _plan()
    b = _plan({**SPLIT, "production": 80, "canary": 20})
    assert a["plan_hash"] != b["plan_hash"]
    assert _plan()["plan_hash"] == a["plan_hash"]  # same world, same plan
    assert plans.compute_hash("set_traffic_split", a["args"], a["preconditions"]) == a["plan_hash"]


def test_concurrent_double_apply_executes_once(monkeypatch):
    plan = _plan()
    calls = []
    spec = plans.plannable_tools()["set_traffic_split"]
    real = spec.fn

    def counting(**kw):
        if plans._MODE.get() == "apply":  # probes also pass through here; count real executions
            calls.append(kw)
            time.sleep(0.05)
        return real(**kw)

    monkeypatch.setattr(
        plans,
        "plannable_tools",
        lambda: {
            "set_traffic_split": type(
                "S",
                (),
                {
                    "fn": staticmethod(counting),
                    "tier": "A",
                    "destructive": True,
                    "name": "set_traffic_split",
                },
            )()
        },
    )
    barrier = threading.Barrier(6)
    results = []

    def go():
        barrier.wait()
        results.append(tools.apply_plan(plan["plan_hash"]))

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(1 for r in results if r["ok"]) == 1, results
    assert len(calls) == 1


def test_apply_reuses_idempotency_keys():
    plan = _plan()
    first = _spec("apply_plan").fn(plan["plan_hash"], idempotency_key="k-1")
    again = _spec("apply_plan").fn(plan["plan_hash"], idempotency_key="k-1")
    assert first["ok"] and again["replayed"] is True


def test_policy_denial_at_plan_time_and_at_apply_time(monkeypatch):
    import examlops.policy as policy

    plan = _plan()
    monkeypatch.setattr(
        policy, "_load_policies", lambda path=None: [{"action": "agent_write", "effect": "deny"}]
    )
    out = tools.apply_plan(plan["plan_hash"])
    assert out["ok"] is False and out["code"] == "policy_denied"
    assert _rules() is None
    assert tools.plan_change("set_traffic_split", SPLIT)["code"] == "policy_denied"


def test_require_approval_needs_a_human_token(monkeypatch, agent):
    import examlops.policy as policy

    monkeypatch.setattr(
        policy,
        "_load_policies",
        lambda path=None: [{"action": "agent_write", "effect": "require_approval"}],
    )
    plan = _plan()
    assert plan["required_approvals"] == ["human_approval"]
    h = plan["plan_hash"]
    assert tools.apply_plan(h)["code"] == "approval_required"
    assert tools.apply_plan(h, "nope")["code"] == "approval_invalid"
    assert tools.approve_plan(h)["code"] == "approval_requires_human"  # agent cannot self-approve
    monkeypatch.delenv("EXAMLOPS_PRINCIPAL_KIND")
    token = tools.approve_plan(h)["approval_token"]
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")
    assert tools.apply_plan(h, "wrong")["code"] == "approval_invalid"
    assert _rules() is None
    out = tools.apply_plan(h, token)
    assert out["ok"], out
    assert _rules()["production"] == 90
    assert "plan_approved" in _actions()


def test_tier_c_cannot_be_planned_by_an_agent(agent):
    out = tools.plan_change(
        "grant_access", {"subject": "u", "relation": "viewer", "obj": "project:x"}
    )
    assert out["code"] == "tier_c_human_only"


def test_invalid_or_unplannable_requests():
    assert tools.plan_change("list_models", {})["code"] == "not_plannable"
    assert tools.plan_change("apply_plan", {})["code"] == "not_plannable"
    assert tools.plan_change("set_traffic_split", {"nope": 1})["code"] == "invalid_args"


def test_probe_never_runs_the_tool_body():
    from examlops.data import init_db

    init_db()
    _plan()
    assert store.list_plans()[0]["state"] == "planned"
    assert _rules() is None


def test_list_and_show():
    h = _plan()["plan_hash"]
    assert plans.list_plans()["plans"][0]["plan_hash"] == h
    assert plans.get_plan(h)["plan"]["tool"] == "set_traffic_split"
    assert plans.get_plan("x")["code"] == "plan_not_found"


def test_every_mutating_tool_is_covered_by_plan_apply():
    """A new mutating ToolSpec must ship with intent, blast radius, preconditions and a gate."""
    names = set(plans.plannable_tools())
    assert names, "no plannable tools"
    assert names == set(plans._INTENT) == set(plans._BLAST) == set(plans._PRECONDITIONS)
    for name, spec in plans.plannable_tools().items():
        # The tool must consult the write gate before its body: probe mode relies on it.
        from tests.unit.test_mcp_agent_write_policy import WRITE_ARGS

        args = plans._normalise(spec, dict(WRITE_ARGS[name]))
        assert plans._probe(spec, args) is not None, f"{name} never reached its write gate"


def test_every_mutating_tool_refuses_a_direct_agent_call(agent):
    from tests.unit.test_mcp_agent_write_policy import WRITE_ARGS

    for name, spec in plans.plannable_tools().items():
        out = spec.fn(**WRITE_ARGS[name])
        assert out["ok"] is False and out["code"] == "plan_required", name
