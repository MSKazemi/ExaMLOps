from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner  # noqa: E402

from examlops.cli.main import app  # noqa: E402

runner = CliRunner()


# ── resources registry ────────────────────────────────────────────────────────


def test_resources_registry():
    from examlops.mcp.resources import iter_resources

    specs = iter_resources()
    uris = {r.uri for r in specs}
    assert "examlops://status" in uris
    assert "examlops://model/{name}" in uris
    # templated detection
    detail = next(r for r in specs if r.uri == "examlops://model/{name}")
    assert detail.templated is True
    status = next(r for r in specs if r.uri == "examlops://status")
    assert status.templated is False
    assert all(r.description for r in specs)


def test_resource_fn_reuses_tools(monkeypatch):
    # examlops://status should return the same envelope shape as the platform_status tool.
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://127.0.0.1:1")
    from examlops.mcp.resources import RESOURCES

    status = next(r for r in RESOURCES if r.uri == "examlops://status")
    out = status.fn()
    assert out["ok"] is False  # unreachable → structured envelope, not an exception


# ── prompts registry ──────────────────────────────────────────────────────────


def test_prompts_registry():
    from examlops.mcp.prompts import iter_prompts

    names = {p.name for p in iter_prompts()}
    assert {"diagnose_drift", "promote_safely", "platform_triage"} <= names
    assert all(p.description for p in iter_prompts())


def test_prompt_text_mentions_model_and_tools():
    from examlops.mcp.prompts import diagnose_drift, promote_safely

    drift = diagnose_drift("JPCP")
    assert "JPCP" in drift
    assert "platform_status" in drift
    promote = promote_safely("JPCP")
    assert "exa pipeline promote jpcp" in promote
    assert "approval gate" in promote.lower()


# ── agent card enrichment ─────────────────────────────────────────────────────


def test_agent_card_includes_resources_and_prompts():
    from examlops.mcp.agent_card import build_agent_card

    card = build_agent_card()
    res_uris = {r["uri"] for r in card["resources"]}
    assert "examlops://models" in res_uris
    assert any(r["templated"] for r in card["resources"])
    prompt_names = {p["name"] for p in card["prompts"]}
    assert "diagnose_drift" in prompt_names


# ── CLI listing ───────────────────────────────────────────────────────────────


def test_cli_mcp_resources_lists():
    result = runner.invoke(app, ["mcp", "resources"])
    assert result.exit_code == 0, result.output
    assert "examlops://status" in result.output
    assert "template" in result.output  # the templated model detail resource


def test_cli_mcp_prompts_lists():
    result = runner.invoke(app, ["mcp", "prompts"])
    assert result.exit_code == 0, result.output
    assert "diagnose_drift" in result.output


def test_cli_mcp_agent_card_json_has_resources():
    result = runner.invoke(app, ["--json", "mcp", "agent-card"])
    assert result.exit_code == 0, result.output
    card = json.loads(result.output)
    assert card["resources"] and card["prompts"]
