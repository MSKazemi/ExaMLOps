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


def test_policy_unavailable_does_not_block(monkeypatch):
    """If the policy layer errors, the gate degrades to allow (write-gate already applied)."""
    import examlops.policy as policy

    def boom(*a, **k):
        raise RuntimeError("policy broken")

    monkeypatch.setattr(policy, "decide", boom)
    assert tools._agent_write_gate("retrain", {"model": "JPCP"}) is None
