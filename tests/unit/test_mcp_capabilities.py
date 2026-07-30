"""Phase 1 (Skipper next-gen) — expanded MCP read surface + capabilities catalogue.

Verifies that the widened ``examlops.mcp.tools.REGISTRY`` covers every ExaMLOps use case,
that every read tool degrades gracefully against an empty ``platform.db`` (returns an
``ok``/``error`` envelope, never raises), that the ``use_cases``/``tier`` metadata is well
formed, and that the capabilities catalogue + A2A card are derived from the one registry.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner  # noqa: E402

from examlops.cli.main import app  # noqa: E402
from examlops.mcp import tools as T  # noqa: E402
from examlops.mcp.agent_card import build_agent_card  # noqa: E402

runner = CliRunner()


@pytest.fixture
def db(tmp_path):
    os.environ["PLATFORM_DB"] = str(tmp_path / "cap.db")
    from examlops.platform_db import init_db

    init_db()
    yield
    os.environ.pop("PLATFORM_DB", None)


# ── metadata invariants ───────────────────────────────────────────────────────


def test_every_tool_has_use_cases_and_valid_tier():
    for spec in T.REGISTRY:
        assert spec.use_cases, f"{spec.name} has no use_cases"
        assert spec.tier in T.TIERS, f"{spec.name} has invalid tier {spec.tier!r}"
        assert all(uc in T.USE_CASES for uc in spec.use_cases), spec.name
        # reads carry the read tier; only mutating tools may be A/B/C
        if not spec.mutating:
            assert spec.tier == "read", spec.name
        else:
            assert spec.tier in {"A", "B", "C"}, spec.name


def test_registry_covers_every_use_case():
    covered = {uc for spec in T.REGISTRY for uc in spec.use_cases}
    assert covered == set(T.USE_CASES)


def test_registry_expanded_beyond_original_surface():
    # Phase 1 widens the read surface well past the original ~20 tools.
    reads = [s for s in T.REGISTRY if not s.mutating]
    assert len(reads) >= 40


@pytest.mark.parametrize(
    "name",
    [
        "drift_status",
        "input_drift_status",
        "traffic_rules",
        "slo_specs",
        "fairness_status",
        "model_costs",
        "carbon",
        "gateway_cache_stats",
        "eval_gate",
        "dataset_revisions",
        "model_lineage",
    ],
)
def test_new_read_tools_are_registered(name):
    assert name in {s.name for s in T.REGISTRY}


# ── graceful degradation: reads never raise ───────────────────────────────────


def test_read_tools_return_envelope_on_empty_db(db):
    # A representative sample across every domain — none may raise, all return ok=True
    # (an empty table is a valid, empty result — not an error).
    assert T.drift_status("jpcp")["ok"] is True
    assert T.list_drift()["ok"] is True
    assert T.traffic_rules("jpcp")["ok"] is True
    assert T.slo_specs()["ok"] is True
    assert T.slo_ratio("jpcp", "latency")["ok"] is True
    assert T.model_costs("jpcp")["ok"] is True
    assert T.platform_cost_summary()["ok"] is True
    assert T.gateway_cache_stats()["ok"] is True
    assert T.eval_results("jpcp")["ok"] is True
    assert T.model_lineage("jpcp")["ok"] is True


def test_redact_drops_sensitive_fields():
    rows = [{"key_hash": "abc", "raw_key": "SECRET", "secret_token": "x", "models": ["jpcp"]}]
    out = T._redact(rows)
    assert out == [{"key_hash": "abc", "models": ["jpcp"]}]


def test_list_gateway_keys_redacts_secrets(db):
    from examlops.data.gateway import create_virtual_key

    create_virtual_key("deadbeef", models=["jpcp"])
    res = T.list_gateway_keys()
    assert res["ok"] is True
    for row in res["keys"]:
        assert not any(s in k.lower() for k in row for s in ("secret", "token", "password"))


def test_explain_command_help_tool():
    # Covers the "help" use case: grounded CLI introspection, no DB needed.
    res = T.explain_command("drift")
    assert res["ok"] is True
    assert res["command"] == "exa drift"
    assert "subcommands" in res
    top = T.explain_command("")
    assert top["ok"] is True and "subcommands" in top
    assert T.explain_command("no-such-cmd")["ok"] is False


# ── capabilities catalogue + card ─────────────────────────────────────────────


def test_capabilities_catalogue_groups_by_use_case():
    cat = T.capabilities_catalogue(include_writes=True)
    assert set(cat).issubset({*T.USE_CASES, "other"})
    for uc in T.USE_CASES:
        assert uc in cat and cat[uc], f"{uc} bucket empty"
    # every entry carries the display fields
    entry = cat["monitoring"][0]
    assert {"name", "description", "mutating", "tier", "tags"} <= set(entry)


def test_capabilities_catalogue_hides_writes_by_default(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_MCP_ALLOW_WRITES", raising=False)
    cat = T.capabilities_catalogue()
    names = {t["name"] for tools in cat.values() for t in tools}
    assert "trigger_retrain" not in names
    assert "drift_status" in names


def test_agent_card_exposes_use_cases_and_grouping():
    card = build_agent_card(include_writes=True)
    assert "capabilitiesByUseCase" in card
    assert all("useCases" in s and "tier" in s for s in card["skills"])


def test_exa_mcp_capabilities_command_runs(db):
    result = runner.invoke(app, ["mcp", "capabilities"])
    assert result.exit_code == 0, result.output
    assert "Monitoring" in result.output


def test_exa_mcp_capabilities_json(db):
    result = runner.invoke(app, ["--json", "mcp", "capabilities", "--all"])
    assert result.exit_code == 0, result.output
    assert "trigger_retrain" in result.output
