"""Unit tests for the MCP project_* tools (Projects & Workspaces, ADR 0086).

Covers read tools (project_list/detail/cost), gated write tools
(project_assign_model/add_member), and their registration in the tool REGISTRY.
"""

from __future__ import annotations

import examlops.mcp.tools as tools


def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    import examlops.platform_db as pdb

    # platform_db caches the resolved path lazily per-call via os.getenv, so setting the
    # env var is enough; force a clean schema.
    pdb.init_db()
    return pdb


def test_project_list_empty(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    out = tools.project_list()
    assert out["ok"] is True
    assert out["projects"] == []


def test_project_list_and_detail(tmp_path, monkeypatch):
    pdb = _fresh_db(tmp_path, monkeypatch)
    pdb.create_project("research", description="R&D", created_by="alice")
    pdb.assign_resource_to_project("research", "model", "JPCP", added_by="alice")

    listed = tools.project_list()
    assert listed["ok"] is True
    assert any(p["name"] == "research" for p in listed["projects"])

    detail = tools.project_detail("research")
    assert detail["ok"] is True
    assert detail["project"]["name"] == "research"

    missing = tools.project_detail("nope")
    assert missing["ok"] is False
    assert "not found" in missing["error"]


def test_project_cost_reports_budget(tmp_path, monkeypatch):
    pdb = _fresh_db(tmp_path, monkeypatch)
    pdb.create_project("research", created_by="alice")
    out = tools.project_cost("research")
    assert out["ok"] is True
    assert "cost" in out and "budget" in out

    assert tools.project_cost("ghost")["ok"] is False


def test_project_assign_model_write(tmp_path, monkeypatch):
    pdb = _fresh_db(tmp_path, monkeypatch)
    pdb.create_project("research", created_by="alice")
    monkeypatch.setenv("EXAMLOPS_ACTOR", "agent-x")

    out = tools.project_assign_model("research", "MACK")
    assert out["ok"] is True
    kinds = pdb.get_project_full("research")["resources"]
    assert "MACK" in kinds.get("model", [])

    # Audited (read back through the MCP audit tool)
    events = tools.recent_audit_events(limit=10)["events"]
    assert any(e["action"] == "project_model_assigned" for e in events)

    # Unknown project → error, not crash
    assert tools.project_assign_model("ghost", "MACK")["ok"] is False


def test_project_add_member_write(tmp_path, monkeypatch):
    pdb = _fresh_db(tmp_path, monkeypatch)
    pdb.create_project("research", created_by="alice")

    bad_role = tools.project_add_member("research", "bob", role="superuser")
    assert bad_role["ok"] is False

    out = tools.project_add_member("research", "bob", role="editor")
    assert out["ok"] is True
    members = pdb.list_project_members("research")
    assert any(m["subject"] == "bob" for m in members)

    assert tools.project_add_member("ghost", "bob")["ok"] is False


def test_write_tools_blocked_by_policy(monkeypatch, tmp_path):
    _fresh_db(tmp_path, monkeypatch)
    import examlops.policy as policy

    monkeypatch.setattr(
        policy, "_load_policies", lambda path=None: [{"action": "agent_write", "effect": "deny"}]
    )
    out = tools.project_assign_model("research", "JPCP")
    assert out["ok"] is False
    assert "policy denied" in out["error"]

    out2 = tools.project_add_member("research", "bob")
    assert out2["ok"] is False


def test_project_tools_registered():
    names = {spec.fn.__name__ for spec in tools.REGISTRY}
    assert {"project_list", "project_detail", "project_cost"} <= names
    # Writes are mutating
    writes = {spec.fn.__name__ for spec in tools.REGISTRY if spec.mutating}
    assert {"project_assign_model", "project_add_member"} <= writes

    # Read-only iteration excludes the mutating project tools
    read_only = {spec.fn.__name__ for spec in tools.iter_tools(include_writes=False)}
    assert "project_list" in read_only
    assert "project_assign_model" not in read_only
