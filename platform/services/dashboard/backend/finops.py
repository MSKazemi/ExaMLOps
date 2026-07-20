"""FinOps + Green-AI aggregators (F13 / ADR 0066).

View-shaped read helpers over the shipped cost/carbon backend (phase 23/24): the ``model_costs``,
``project_budgets``, and ``carbon_records`` tables. They back the FinOps console — cost rollups,
budget-vs-actual, carbon accounting (with honest uncertainty), and unit economics — without
recomputing anything at request time.

**Honest estimation (F13 R3).** Carbon figures are estimates; every carbon payload carries a
``methodology`` string and an ``uncertainty`` fraction so the UI never shows false precision.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from dbconn import connect

# Carbon estimation is inherently approximate (grid intensity varies hourly, TDP ≠ actual draw).
# Surface a coarse relative uncertainty so the UI can show error bars rather than false precision.
# These are the fallback figures for the platform's default methodology; when records were produced
# by a pluggable provider (ADR 0074) we prefer that provider's own methodology/uncertainty.
CARBON_UNCERTAINTY = 0.30  # ±30%
CARBON_METHODOLOGY = (
    "Energy = GPU-hours × TDP × PUE; CO₂e = energy × grid intensity. "
    "Grid intensity and TDP are estimates; treat figures as ±30%."
)


def _provider_metadata(provider_name: str) -> tuple[str | None, float | None]:
    """Best-effort (methodology, uncertainty) for a carbon provider name (ADR 0074).

    Imported lazily and defensively: the dashboard service does not depend on the ``examlops`` CLI
    package, so when it is unavailable we fall back to the platform defaults rather than failing.
    """
    try:
        import examlops.finops.carbon_providers  # noqa: F401 - registers the built-ins
        from examlops.providers import get_provider

        meta = get_provider("carbon", provider_name).metadata()
        return meta.methodology or None, meta.uncertainty
    except Exception:
        return None, None


def _connect(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


# ── cost rollup (F13 R1) ──────────────────────────────────────────────────────


def cost_rollup(db_path: str) -> dict[str, Any]:
    """GPU-hour + USD spend rolled up per model, plus facility totals (F13 R1)."""
    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "model_costs"):
            return {"rows": [], "total_gpu_hours": 0.0, "total_cost_usd": 0.0}
        rows = [
            {
                "dimension": "model",
                "key": r["model_name"],
                "gpuHours": round(r["gh"] or 0.0, 3),
                "costUsd": round(r["c"] or 0.0, 2),
                "runs": r["n"],
            }
            for r in conn.execute(
                "SELECT model_name, COUNT(*) AS n, SUM(gpu_hours) AS gh, SUM(cost_usd) AS c "
                "FROM model_costs GROUP BY model_name ORDER BY c DESC"
            )
        ]
        total_gh = round(sum(r["gpuHours"] for r in rows), 3)
        total_cost = round(sum(r["costUsd"] for r in rows), 2)
        return {"rows": rows, "total_gpu_hours": total_gh, "total_cost_usd": total_cost}
    finally:
        conn.close()


# ── budget vs actual (F13 R2) ─────────────────────────────────────────────────


def _usage_ratio(consumed: float, budget: float | None) -> float | None:
    if budget is None:
        return None
    if budget == 0:
        return float("inf") if consumed > 0 else 0.0
    return round(consumed / budget, 3)


def budget_status(db_path: str) -> dict[str, Any]:
    """Per-project budget vs facility-wide actuals, with an over-budget flag (F13 R2).

    ``model_costs`` has no project column yet, so consumed is the facility total charged against each
    configured budget; per-project attribution lands with the F15 tenant columns.
    """
    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "project_budgets"):
            return {"budgets": [], "consumed_gpu_hours": 0.0, "consumed_cost_usd": 0.0}
        consumed_gh = consumed_cost = 0.0
        if _table_exists(conn, "model_costs"):
            row = conn.execute(
                "SELECT COALESCE(SUM(gpu_hours),0) AS gh, COALESCE(SUM(cost_usd),0) AS c FROM model_costs"
            ).fetchone()
            consumed_gh, consumed_cost = row["gh"], row["c"]
        budgets = []
        for b in conn.execute(
            "SELECT project, gpu_hours_budget, cost_budget, period FROM project_budgets ORDER BY project"
        ):
            gh_ratio = _usage_ratio(consumed_gh, b["gpu_hours_budget"])
            cost_ratio = _usage_ratio(consumed_cost, b["cost_budget"])
            over = any(r is not None and r > 1.0 for r in (gh_ratio, cost_ratio))
            budgets.append(
                {
                    "project": b["project"],
                    "period": b["period"],
                    "gpuHoursBudget": b["gpu_hours_budget"],
                    "costBudget": b["cost_budget"],
                    "gpuHoursRatio": gh_ratio,
                    "costRatio": cost_ratio,
                    "overBudget": over,
                }
            )
        return {
            "budgets": budgets,
            "consumed_gpu_hours": round(consumed_gh, 3),
            "consumed_cost_usd": round(consumed_cost, 2),
        }
    finally:
        conn.close()


# ── carbon / Green-AI (F13 R3) ────────────────────────────────────────────────


def carbon_summary(db_path: str) -> dict[str, Any]:
    """Total energy + CO₂e from ``carbon_records``, with methodology + uncertainty (F13 R3)."""
    conn = _connect(db_path)
    try:
        totals = {"kwh": 0.0, "co2e_g": 0.0, "records": 0}
        by_model: list[dict[str, Any]] = []
        providers: list[str] = []
        if _table_exists(conn, "carbon_records"):
            r = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(kwh),0) AS kwh, COALESCE(SUM(co2e_g),0) AS c "
                "FROM carbon_records"
            ).fetchone()
            totals = {"kwh": round(r["kwh"], 3), "co2e_g": round(r["c"], 1), "records": r["n"]}
            by_model = [
                {
                    "model": m["model"],
                    "kwh": round(m["kwh"] or 0.0, 3),
                    "co2e_g": round(m["c"] or 0.0, 1),
                }
                for m in conn.execute(
                    "SELECT model, SUM(kwh) AS kwh, SUM(co2e_g) AS c FROM carbon_records "
                    "GROUP BY model ORDER BY c DESC"
                )
            ]
            # Which pluggable providers produced these records (ADR 0074)? Guarded — older DBs may
            # predate the `provider` column.
            cols = {row[1] for row in conn.execute("PRAGMA table_info(carbon_records)")}
            if "provider" in cols:
                providers = [
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT provider FROM carbon_records WHERE provider IS NOT NULL "
                        "ORDER BY provider"
                    )
                ]
        # When every record came from a single provider, surface that provider's own methodology +
        # uncertainty; otherwise fall back to the platform defaults (mixed/legacy records).
        methodology, uncertainty = CARBON_METHODOLOGY, CARBON_UNCERTAINTY
        if len(providers) == 1:
            m, u = _provider_metadata(providers[0])
            if m is not None:
                methodology = m
            if u is not None:
                uncertainty = u
        return {
            "totals": totals,
            "byModel": by_model,
            "providers": providers,
            "co2e_kg": round(totals["co2e_g"] / 1000.0, 2),
            "uncertainty": uncertainty,
            "methodology": methodology,
        }
    finally:
        conn.close()


# ── unit economics (F13 R4) ───────────────────────────────────────────────────


def unit_economics(db_path: str) -> dict[str, Any]:
    """Cost-per-training-run and cost-per-inference (F13 R4)."""
    conn = _connect(db_path)
    try:
        cost_per_run = None
        if _table_exists(conn, "model_costs"):
            r = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS c FROM model_costs"
            ).fetchone()
            if r["n"]:
                cost_per_run = round(r["c"] / r["n"], 2)

        cost_per_inference = None
        if _table_exists(conn, "inference_energy") and _table_exists(conn, "carbon_records"):
            reqs = conn.execute(
                "SELECT COALESCE(SUM(requests),0) AS n FROM inference_energy"
            ).fetchone()["n"]
            # Approximate inference cost via energy → USD is out of scope; report request volume only.
            cost_per_inference = None if not reqs else {"requests": reqs}

        return {"costPerTrainingRun": cost_per_run, "inference": cost_per_inference}
    finally:
        conn.close()
