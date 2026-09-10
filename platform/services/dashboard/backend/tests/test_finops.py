"""FinOps + Green-AI aggregators + /api/v1/finops/overview (F13 / ADR 0066)."""

import dbconn
import finops
import pytest

from examlops import platform_db as pdb
from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    conn = dbconn.connect(db, row_factory=None)
    conn.executemany(
        "INSERT INTO model_costs (model_name, version, gpu_hours, cost_usd, recorded_at) VALUES (?,?,?,?,?)",
        [
            ("jpcp", 18, 4.0, 10.0, "2026-07-01"),
            ("jpcp", 19, 2.0, 6.0, "2026-07-02"),
            ("demo", 1, 1.0, 4.0, "2026-07-02"),
        ],
    )
    conn.execute(
        "INSERT INTO project_budgets (project, gpu_hours_budget, cost_budget, period) "
        "VALUES ('eu-hpc', 10.0, 15.0, 'monthly')"  # consumed 7 gh / 20 usd → cost over budget
    )
    conn.executemany(
        "INSERT INTO carbon_records (model, kwh, co2e_g, grid_intensity) VALUES (?,?,?,?)",
        [("jpcp", 12.0, 3600.0, 300.0), ("demo", 3.0, 900.0, 300.0)],
    )
    conn.commit()
    conn.close()
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


# ── cost rollup (F13 R1) ─────────────────────────────────────────────────────


def test_cost_rollup_aggregates_per_model(platform_db):
    out = finops.cost_rollup(platform_db)
    assert out["total_gpu_hours"] == 7.0
    assert out["total_cost_usd"] == 20.0
    jpcp = next(r for r in out["rows"] if r["key"] == "jpcp")
    assert jpcp["gpuHours"] == 6.0
    assert jpcp["costUsd"] == 16.0
    assert jpcp["runs"] == 2
    # rows sorted by cost desc → jpcp first
    assert out["rows"][0]["key"] == "jpcp"


def test_cost_rollup_graceful_without_table(tmp_path, monkeypatch):
    db = tmp_path / "e.db"
    dbconn.connect(db, row_factory=None).close()
    out = finops.cost_rollup(str(db))
    assert out["rows"] == [] and out["total_cost_usd"] == 0.0


# ── budget vs actual (F13 R2) ────────────────────────────────────────────────


def test_budget_status_flags_overspend(platform_db):
    out = finops.budget_status(platform_db)
    eu = out["budgets"][0]
    assert eu["project"] == "eu-hpc"
    # consumed cost 20 > budget 15 → over
    assert eu["costRatio"] > 1.0
    assert eu["overBudget"] is True
    assert out["consumed_cost_usd"] == 20.0


# ── carbon (F13 R3) ──────────────────────────────────────────────────────────


def test_carbon_summary_totals_and_uncertainty(platform_db):
    out = finops.carbon_summary(platform_db)
    assert out["totals"]["co2e_g"] == 4500.0
    assert out["co2e_kg"] == 4.5
    # ADR 0112 R-ee: labelled operational, embodied unavailable — the UI shows scopeNote
    assert out["scope"] == "operational" and out["embodiedKg"] is None
    assert "not a total" in out["scopeNote"]
    # honest estimation: uncertainty + methodology present (no false precision)
    assert out["uncertainty"] == finops.CARBON_UNCERTAINTY
    assert "±30%" in out["methodology"]
    assert out["byModel"][0]["model"] == "jpcp"  # highest co2e first
    # legacy records (no provider column populated) → fall back to platform defaults
    assert out["providers"] == []


def test_carbon_summary_uses_provider_methodology(tmp_path, monkeypatch):
    # Records tagged with a single pluggable provider → surface that provider's own methodology +
    # uncertainty instead of the platform defaults (ADR 0074 / S4).
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    conn = dbconn.connect(db, row_factory=None)
    conn.executemany(
        "INSERT INTO carbon_records (model, kwh, co2e_g, grid_intensity, provider) VALUES (?,?,?,?,?)",
        [("jpcp", 6.0, 900.0, 250.0, "codecarbon-like")],
    )
    conn.commit()
    conn.close()
    out = finops.carbon_summary(str(db))
    assert out["providers"] == ["codecarbon-like"]
    # codecarbon-like advertises ±25% and a CodeCarbon-style methodology (not the ±30% default)
    assert out["uncertainty"] == 0.25
    assert "CodeCarbon" in out["methodology"]


# ── unit economics (F13 R4) ──────────────────────────────────────────────────


def test_unit_economics_cost_per_run(platform_db):
    out = finops.unit_economics(platform_db)
    assert out["costPerTrainingRun"] == round(20.0 / 3, 2)


# ── endpoint ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finops_endpoint_requires_auth(client, platform_db):
    r = await client.get("/api/v1/finops/overview")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_finops_endpoint_composes_all_sources(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/finops/overview", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["cost"]["total_cost_usd"] == 20.0
    assert body["budget"]["budgets"][0]["overBudget"] is True
    assert body["carbon"]["co2e_kg"] == 4.5
    assert "unitEconomics" in body
