from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner  # noqa: E402

from examlops.cli.main import app  # noqa: E402

runner = CliRunner()


@pytest.fixture
def db(tmp_path):
    os.environ["PLATFORM_DB"] = str(tmp_path / "mcp.db")
    from examlops.platform_db import init_db

    init_db()
    yield
    os.environ.pop("PLATFORM_DB", None)


# ── tools registry (pure, no fastmcp) ─────────────────────────────────────────


def test_registry_has_read_and_write_tools():
    from examlops.mcp.tools import REGISTRY

    assert any(s.mutating for s in REGISTRY)
    assert any(not s.mutating for s in REGISTRY)
    # every tool has a non-empty description derived from its docstring
    assert all(s.description for s in REGISTRY)


def test_iter_tools_hides_writes_by_default(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_MCP_ALLOW_WRITES", raising=False)
    from examlops.mcp.tools import iter_tools

    names = {s.name for s in iter_tools()}
    assert "trigger_retrain" not in names
    assert "platform_status" in names


def test_iter_tools_shows_writes_when_enabled(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MCP_ALLOW_WRITES", "1")
    from examlops.mcp.tools import iter_tools

    names = {s.name for s in iter_tools()}
    assert "trigger_retrain" in names


# ── HPC fleet tools (Phase 35d) ───────────────────────────────────────────────


def test_hpc_read_tools_registered_and_writes_gated(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_MCP_ALLOW_WRITES", raising=False)
    from examlops.mcp.tools import iter_tools

    names = {s.name for s in iter_tools()}
    assert {"hpc_clusters", "hpc_nodes", "hpc_place", "hpc_jobs"} <= names
    assert "hpc_approve_cluster" not in names  # mutating, hidden by default

    monkeypatch.setenv("EXAMLOPS_MCP_ALLOW_WRITES", "1")
    assert "hpc_approve_cluster" in {s.name for s in iter_tools()}


def test_hpc_clusters_and_approve_via_mcp(db, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_HPC_REGISTRY", os.environ["PLATFORM_DB"] + ".yaml")
    from examlops.hpc_registry import register_pending
    from examlops.mcp.tools import hpc_approve_cluster, hpc_clusters

    register_pending("lxp", "flux", host="lxp-login")
    res = hpc_clusters()
    assert res["ok"] and res["clusters"][0]["state"] == "PENDING"

    approved = hpc_approve_cluster("lxp")
    assert approved["ok"] and approved["state"] == "ACTIVE"
    assert hpc_approve_cluster("ghost")["ok"] is False


def test_hpc_place_tool_no_clusters(db):
    from examlops.mcp.tools import hpc_place

    res = hpc_place(gpus=4)
    assert res["ok"] is True
    assert res["cluster"] is None  # nothing ACTIVE yet


def test_trigger_retrain_without_token_returns_error_envelope(monkeypatch, tmp_path):
    monkeypatch.delenv("CONTROL_PLANE_TOKEN", raising=False)
    # Hermetic: point config resolution at an empty file so the developer's real
    # ~/.config/examlops/config.toml (which may hold a token) can't leak in.
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "empty.toml"))
    from examlops.mcp.tools import trigger_retrain

    res = trigger_retrain("JPCP", "PM100Dataset", dummy=True)
    assert res["ok"] is False
    assert "CONTROL_PLANE_TOKEN" in res["error"]


def test_platform_status_unreachable_returns_error_envelope(monkeypatch):
    # Point at a closed port so the client raises, and the tool returns an envelope.
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://127.0.0.1:1")
    from examlops.mcp.tools import platform_status

    res = platform_status()
    assert res["ok"] is False
    assert "error" in res


def test_recent_audit_events_reads_db(db):
    from examlops.mcp.tools import recent_audit_events
    from examlops.platform_db import write_audit_event

    write_audit_event("cli", "alice", "retrain_triggered", "JPCP", {"i": 1})
    res = recent_audit_events(limit=5)
    assert res["ok"] is True
    assert res["count"] >= 1
    assert res["events"][0]["target"] == "JPCP"


# ── agent card ────────────────────────────────────────────────────────────────


def test_agent_card_lists_skills():
    from examlops.mcp.agent_card import build_agent_card

    card = build_agent_card(base_url="https://example.org", include_writes=True)
    assert card["name"] == "ExaMLOps"
    assert card["url"] == "https://example.org"
    ids = {s["id"] for s in card["skills"]}
    assert "platform_status" in ids
    assert "trigger_retrain" in ids  # writes included
    assert card["interfaces"]["mcp"]["transports"] == ["stdio", "http"]


# ── CLI wiring ────────────────────────────────────────────────────────────────


def test_cli_mcp_tools_lists_read_tools():
    result = runner.invoke(app, ["mcp", "tools"])
    assert result.exit_code == 0, result.output
    assert "platform_status" in result.output


def test_cli_mcp_tools_all_shows_writes():
    result = runner.invoke(app, ["mcp", "tools", "--all"])
    assert result.exit_code == 0, result.output
    assert "trigger_retrain" in result.output


def test_cli_mcp_agent_card_json():
    result = runner.invoke(app, ["--json", "mcp", "agent-card"])
    assert result.exit_code == 0, result.output
    card = json.loads(result.output)
    assert card["name"] == "ExaMLOps"
    assert card["skills"]


def test_cli_mcp_serve_without_fastmcp_errors_cleanly(monkeypatch):
    # Simulate FastMCP not being installed: force the import path to raise.
    import examlops.mcp.server as srv

    def _boom():
        raise srv.FastMCPNotInstalled()

    monkeypatch.setattr(srv, "_import_fastmcp", _boom)
    result = runner.invoke(app, ["mcp", "serve", "--transport", "http"])
    assert result.exit_code == 1
    assert "FastMCP is not installed" in result.output


def test_fleet_simulate_tool(db):
    from examlops.mcp.tools import fleet_simulate

    # No clusters registered → jobs project to the queue, but the tool returns a valid envelope.
    res = fleet_simulate(jobs=3, gpus=2)
    assert res["ok"] is True
    assert "projected" in res and res["projected"]["queued"] == 3


def test_fleet_simulate_is_registered_as_read_tool():
    from examlops.mcp.tools import REGISTRY

    spec = next(s for s in REGISTRY if s.name == "fleet_simulate")
    assert spec.mutating is False and "fleet" in spec.tags


def test_agent_card_advertises_security_schemes(monkeypatch):
    """A2A card must declare securitySchemes (item 2.2) — not imply it's unauthenticated."""
    from examlops.mcp.agent_card import build_agent_card

    monkeypatch.delenv("EXAMLOPS_OIDC_ISSUER", raising=False)
    card = build_agent_card(base_url="https://x")
    assert "securitySchemes" in card
    assert card["securitySchemes"]["default"]["scheme"] == "bearer"

    monkeypatch.setenv("EXAMLOPS_OIDC_ISSUER", "https://idp.example.org/")
    oidc_card = build_agent_card(base_url="https://x")
    scheme = oidc_card["securitySchemes"]["default"]
    assert scheme["type"] == "openIdConnect"
    assert "openid-configuration" in scheme["openIdConnectUrl"]
