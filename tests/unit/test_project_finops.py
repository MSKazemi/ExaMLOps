"""P4 — Project FinOps & monitoring attribution (ADR 0089, spec P4).

GWT-1 direct model_costs.project attribution · GWT-2 union fallback preserved ·
GWT-3 budget breach flagged + audited · quota surfaced.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import project_finops as pf  # noqa: E402
from examlops.platform_db import (  # noqa: E402
    assign_resource_to_project,
    create_project,
    get_db,
    init_db,
    record_model_cost,
    set_project_budget,
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "true")
    init_db()
    create_project("research")


def test_gwt1_direct_attribution():
    assign_resource_to_project("research", "model", "JPCP")
    record_model_cost("JPCP", 1, None, None, 2.5, 10.0)  # auto-attributed to 'research'
    s = pf.cost_summary("research")
    assert s["gpu_hours"] == pytest.approx(2.5)
    assert s["cost_usd"] == pytest.approx(10.0)
    assert s["records"] == 1


def test_gwt2_union_fallback_for_legacy_namespace_model():
    with get_db() as c:
        c.execute("INSERT INTO namespace_models (namespace, model) VALUES ('research','LEGACY')")
    record_model_cost("LEGACY", 1, None, None, 3.0, 12.0, project=None)  # no project column
    s = pf.cost_summary("research")
    # direct column path sees 0 (no project set), but the union figure still counts it
    assert s["union_gpu_hours"] == pytest.approx(3.0)


def test_carbon_attributed():
    assign_resource_to_project("research", "model", "JPCP")
    record_model_cost("JPCP", 1, None, None, 1.0, 5.0)
    with get_db() as c:
        c.execute("INSERT INTO carbon_records (model, kwh, co2e_g) VALUES ('JPCP', 1.0, 42.0)")
    s = pf.cost_summary("research")
    assert s["carbon_grams_co2e"] == pytest.approx(42.0)


def test_gwt3_budget_breach_flagged_and_audited():
    assign_resource_to_project("research", "model", "JPCP")
    record_model_cost("JPCP", 1, None, None, 10.0, 100.0)
    set_project_budget("research", 5.0, 50.0)
    st = pf.budget_status("research", actor="tester", audit=True)
    assert st["over_budget"] is True
    assert len(st["breaches"]) == 2
    with get_db() as c:
        assert c.execute(
            "SELECT 1 FROM audit_events WHERE action='project_budget_breach' AND target='research'"
        ).fetchone()


def test_budget_within_limit_not_breached():
    assign_resource_to_project("research", "model", "JPCP")
    record_model_cost("JPCP", 1, None, None, 1.0, 5.0)
    set_project_budget("research", 100.0, 100.0)
    st = pf.budget_status("research")
    assert st["over_budget"] is False
    assert st["breaches"] == []


def test_quota_surfaced():
    st = pf.budget_status("research")
    assert st["quota"] is not None and "gpu_limit" in st["quota"]
