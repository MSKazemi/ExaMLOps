"""Phase 5 (Skipper next-gen) — MCP write surface + layered safety tiers (ADR 0102).

Verifies the new gated config-write tools are registered with tiers, that the Skipper bridge
HITL-wraps every mutating tool (closing the gap where bridged writes skipped confirmation), that
Tier-C tools are never bound to the autonomous agent, and that the red-team invariant holds — no
dangerous tool the agent can call is left unguarded.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CLI_SRC = Path(__file__).resolve().parents[3] / "platform" / "cli" / "src"
sys.path.insert(0, str(_CLI_SRC))

from examlops.mcp import tools as T  # noqa: E402


def test_write_tools_registered_with_tiers():
    by_name = {s.name: s for s in T.REGISTRY}
    for name in (
        "set_traffic_split",
        "disable_challenger",
        "set_drift_autoretrain",
        "set_promotion_rule",
        "grant_access",
    ):
        assert name in by_name, name
        assert by_name[name].mutating is True
        assert by_name[name].tier in {"A", "B", "C"}
    assert by_name["set_traffic_split"].tier == "A"
    assert by_name["set_promotion_rule"].tier == "B"
    assert by_name["grant_access"].tier == "C"


def test_writes_hidden_unless_enabled(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_MCP_ALLOW_WRITES", raising=False)
    names = {s.name for s in T.iter_tools()}
    assert "set_traffic_split" not in names
    monkeypatch.setenv("EXAMLOPS_MCP_ALLOW_WRITES", "1")
    names = {s.name for s in T.iter_tools()}
    assert "set_traffic_split" in names


def test_write_gate_blocks_when_policy_denies(monkeypatch, tmp_path):
    # With writes enabled but a policy that denies, the tool returns an error envelope.
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "w.db"))

    class _Denied:
        denied = True
        requires_approval = False
        reason = "test-deny"

    import examlops.policy as policy

    monkeypatch.setattr(policy, "decide", lambda *a, **k: _Denied())
    res = T.set_traffic_split("jpcp", 90, 10)
    assert res["ok"] is False and "denied" in res["error"]


def test_traffic_split_validates_sum(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "w2.db"))
    monkeypatch.setattr("examlops.mcp.tools._agent_write_gate", lambda *a, **k: None)
    assert T.set_traffic_split("jpcp", 80, 10)["ok"] is False  # 90 != 100


# ── Skipper bridge: HITL-wrap + tier-C exclusion + red-team invariant ─────────


def test_bridge_hitl_wraps_writes_and_excludes_tier_c(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MCP_ALLOW_WRITES", "1")
    from skipper.confirm import WRITE_TOOLS
    from skipper.tools.mcp_bridge import mcp_tools

    tools = mcp_tools(include_writes=True)
    names = {t.name for t in tools}
    # tier A/B mutating tools are bound AND registered as confirmation-gated writes
    assert "set_traffic_split" in names
    assert "set_traffic_split" in WRITE_TOOLS
    assert "set_promotion_rule" in WRITE_TOOLS
    # tier C is never bound to the autonomous agent
    assert "grant_access" not in names


def test_red_team_invariant_holds_for_mcp_writes(monkeypatch):
    # Every dangerous tool the agent can call must be in the confirmation-gated set.
    monkeypatch.setenv("EXAMLOPS_MCP_ALLOW_WRITES", "1")
    from skipper.confirm import WRITE_TOOLS
    from skipper.memory_eval import unguarded_write_tools
    from skipper.tools.mcp_bridge import mcp_tools

    mcp_tools(include_writes=True)  # populates WRITE_TOOLS via the HITL wrap
    # importing the tool modules registers their confirmed_write / gated writes in WRITE_TOOLS
    import skipper.tools  # noqa: F401
    import skipper.tools.memory  # noqa: F401  (record_procedure is gated here)

    unguarded = unguarded_write_tools(WRITE_TOOLS)
    assert unguarded == [], f"unguarded dangerous tools: {unguarded}"
