"""Unit tests for least-privilege agent-write policy on MCP tools (INC-4 / ADR 0082 layer 2)."""

from __future__ import annotations

import examlops.mcp.tools as tools


def test_gate_allows_when_no_policy(monkeypatch):
    """No policy ⇒ allow ⇒ gate returns None (backward compatible)."""
    import examlops.policy as policy

    monkeypatch.setattr(policy, "_load_policies", lambda path=None: [])
    assert tools._agent_write_gate("retrain", {"model": "JPCP"}) is None


def test_gate_denies_on_deny_rule(monkeypatch):
    import examlops.policy as policy

    monkeypatch.setattr(
        policy, "_load_policies", lambda path=None: [{"action": "agent_write", "effect": "deny"}]
    )
    out = tools._agent_write_gate("retrain", {"model": "JPCP"})
    assert out is not None and out["ok"] is False
    assert "policy denied" in out["error"]


def test_gate_blocks_require_approval_for_agent(monkeypatch):
    """require_approval must NOT be auto-permitted to an agent (no human at the tool boundary)."""
    import examlops.policy as policy

    monkeypatch.setattr(
        policy,
        "_load_policies",
        lambda path=None: [{"action": "agent_write", "effect": "require_approval"}],
    )
    out = tools._agent_write_gate("approve_cluster", {"target": "lxp"})
    assert out is not None and out["ok"] is False
    assert "human approval" in out["error"]


def test_gate_allows_retrain_but_denies_others(monkeypatch):
    """Classic least-privilege policy: agents may retrain, nothing else."""
    import examlops.policy as policy

    rules = [
        {"action": "agent_write", "when": "action_kind == 'retrain'", "effect": "allow"},
        {"action": "agent_write", "effect": "deny"},
    ]
    monkeypatch.setattr(policy, "_load_policies", lambda path=None: rules)
    assert tools._agent_write_gate("retrain", {"model": "JPCP"}) is None
    blocked = tools._agent_write_gate("approve_cluster", {"target": "lxp"})
    assert blocked is not None and blocked["ok"] is False


def test_trigger_retrain_blocked_by_policy(monkeypatch):
    """The mutating tool itself refuses when policy denies — before any control-plane call."""
    import examlops.policy as policy

    monkeypatch.setattr(
        policy, "_load_policies", lambda path=None: [{"action": "agent_write", "effect": "deny"}]
    )
    out = tools.trigger_retrain("JPCP", "PM100Dataset", dummy=True)
    assert out["ok"] is False
    assert "policy denied" in out["error"]


def test_policy_unavailable_fails_closed(monkeypatch):
    """A broken policy layer must never grant an agent permission by accident."""
    import examlops.policy as policy

    def boom(*a, **k):
        raise RuntimeError("policy broken")

    monkeypatch.setattr(policy, "decide", boom)
    out = tools._agent_write_gate("retrain", {"model": "JPCP"})
    assert out is not None and out["ok"] is False
    assert "policy unavailable" in out["error"]


# ── the guard the tests above do not give: *every* mutating tool, not one of them ──────────
#
# The tests above prove the gate's logic, and prove that `trigger_retrain` honours it. They say
# nothing about the other eight mutating tools. A new one registered without a `_agent_write_gate`
# call — or an old one where the gate is added after the first side effect — would ship silently,
# because nothing walks the registry. This does.

import pytest  # noqa: E402

#: Harmless arguments per mutating tool. A tool must refuse *before* touching anything, so these
#: values are never expected to reach a control plane, a database or a cluster.
WRITE_ARGS: dict[str, dict[str, object]] = {
    "trigger_retrain": {"model_name": "JPCP", "dataset_name": "PM100Dataset", "dummy": True},
    "operation_cancel": {"operation_id": "guard-operation"},
    "hpc_approve_cluster": {"name": "guard-cluster"},
    "project_assign_model": {"project": "guard-proj", "model": "JPCP"},
    "project_add_member": {"project": "guard-proj", "subject": "guard-user", "role": "viewer"},
    "set_traffic_split": {"model": "JPCP", "production": 90, "canary": 10},
    "disable_challenger": {"model": "JPCP"},
    "set_drift_autoretrain": {"model": "JPCP", "dataset": "PM100Dataset", "enabled": True},
    "set_promotion_rule": {"model": "JPCP", "metric": "rmse", "operator": "<", "threshold": 5.0},
    "grant_access": {"subject": "guard-user", "relation": "viewer", "obj": "project:guard-proj"},
    "dataplane_pull": {"name": "does-not-exist"},
    "gateway_service_reload": {},
    "genai_app_invoke": {"ref": "does-not-exist", "message": "guard probe"},
}

# plan/apply/approve delegate to each tool's own gate (probe mode / apply) — see test_plan_apply.py.
MUTATING = [
    spec
    for spec in tools.REGISTRY
    if spec.mutating and spec.name not in {"plan_change", "apply_plan", "approve_plan"}
]


def _write_args(spec) -> dict[str, object]:
    """Arguments for ``spec`` — failing loudly when a new mutating tool has none.

    This is what makes the guard self-extending: registering a mutating tool without adding it
    here turns this file red, which is the moment to decide how it should be gated.
    """
    name = spec.fn.__name__
    if name not in WRITE_ARGS:
        pytest.fail(
            f"mutating tool {name!r} has no entry in WRITE_ARGS — add harmless arguments so the "
            f"write-gate guard can prove it refuses a denied policy"
        )
    return WRITE_ARGS[name]


def test_every_mutating_tool_is_actually_gated():
    """No tool may be registered as mutating without a gate call — a static, cheap first pass."""
    import inspect

    ungated = [
        spec.fn.__name__
        for spec in MUTATING
        if "_agent_write_gate" not in inspect.getsource(spec.fn)
    ]
    assert not ungated, f"mutating tools that never consult the write gate: {ungated}"


@pytest.mark.parametrize("spec", MUTATING, ids=lambda s: s.fn.__name__)
def test_mutating_tool_refuses_a_denied_policy(spec, monkeypatch, tmp_path):
    """Deny every agent write, then call the tool for real: it must refuse, not act."""
    import examlops.policy as policy

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setattr(
        policy, "_load_policies", lambda path=None: [{"action": "agent_write", "effect": "deny"}]
    )
    out = spec.fn(**_write_args(spec))
    assert out["ok"] is False, f"{spec.fn.__name__} did not refuse a denied write: {out}"
    assert "policy denied" in out["error"]


@pytest.mark.parametrize("spec", MUTATING, ids=lambda s: s.fn.__name__)
def test_mutating_tool_refuses_when_a_human_must_approve(spec, monkeypatch, tmp_path):
    """``require_approval`` has no human at the tool boundary — every tool must decline it."""
    import examlops.policy as policy

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setattr(
        policy,
        "_load_policies",
        lambda path=None: [{"action": "agent_write", "effect": "require_approval"}],
    )
    out = spec.fn(**_write_args(spec))
    assert out["ok"] is False, f"{spec.fn.__name__} acted despite require_approval: {out}"
    assert "human approval" in out["error"]
