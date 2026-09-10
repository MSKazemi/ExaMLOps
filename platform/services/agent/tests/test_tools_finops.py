"""FinOps agent tools — the finops specialist must be able to answer cost/carbon/budget."""

import importlib
import json

import pytest


@pytest.fixture()
def finops_tools(tmp_path, monkeypatch):
    """Reload the tool module against a private PLATFORM_DB seeded with FinOps data."""
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))

    import examlops.platform_db as pdb

    importlib.reload(pdb)
    pdb.init_db(force=True)
    pdb.record_model_cost("jpcp", 1, None, "42", gpu_hours=2.0, cost_usd=10.0)
    pdb.record_model_cost("mack", 1, None, "43", gpu_hours=1.0, cost_usd=3.0)
    pdb.write_carbon_record("jpcp", None, kwh=1.0, co2e_g=500.0, provider="green-ai-default")
    pdb.create_project("research")
    pdb.assign_model_to_project("research", "jpcp")
    pdb.set_project_budget("research", gpu_hours_budget=None, cost_budget=5.0)

    from skipper.tools import finops

    return importlib.reload(finops)


def test_cost_summary_rolls_up_per_model(finops_tools):
    out = json.loads(finops_tools.get_cost_summary.invoke({}))
    by_name = {m["model_name"]: m for m in out["models"]}
    assert by_name["jpcp"]["cost_usd"] == 10.0
    assert by_name["mack"]["gpu_hours"] == 1.0


def test_model_cost_history(finops_tools):
    rows = json.loads(finops_tools.get_model_cost_history.invoke({"model_name": "jpcp"}))
    assert len(rows) == 1 and float(rows[0]["cost_usd"]) == 10.0


def test_model_cost_history_empty(finops_tools):
    out = finops_tools.get_model_cost_history.invoke({"model_name": "nope"})
    assert "No cost records" in out


def test_carbon_summary_totals(finops_tools):
    out = json.loads(finops_tools.get_carbon_summary.invoke({"model_name": "jpcp"}))
    assert out["operational_kg_co2e"] == 0.5
    # ADR 0112 R-ee: the agent is never handed an operational sum labelled as a total
    assert out["total_kg_co2e"] is None and out["scope"] == "operational"
    assert out["records"][0]["provider"] == "green-ai-default"


def test_budget_status_flags_breach(finops_tools):
    out = json.loads(finops_tools.get_budget_status.invoke({"project": "research"}))
    assert out[0]["project"] == "research"
    assert out[0]["breached"] is True  # spent 10.0 > 5.0 budget
    assert out[0]["consumption"]["cost_usd"] == 10.0


def test_tools_registered_in_agent_and_specialist():
    from skipper import skills
    from skipper.tools import TOOLS

    names = {getattr(t, "name", "") for t in TOOLS}
    finops_pack = next(s for s in skills.SPECIALISTS if s.name == "finops")
    for tool_name in (
        "get_cost_summary",
        "get_model_cost_history",
        "get_carbon_summary",
        "get_budget_status",
    ):
        assert tool_name in names
        assert tool_name in finops_pack.inrepo
