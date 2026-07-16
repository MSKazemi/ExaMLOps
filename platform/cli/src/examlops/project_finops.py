"""P4 — Project FinOps & monitoring attribution (ADR 0089).

Completes the deferred per-project cost attribution (finops.py:102): sums cost/carbon directly by
the ``model_costs.project`` column (populated by P1/P3), and compares consumption against the
project's budget and GPU quota to flag breaches. Read-only aggregation over ``platform.db`` — no
schema changes; degrades cleanly when nothing is recorded yet.
"""

from __future__ import annotations

from typing import Any

from examlops.platform_db import (
    get_db,
    get_project,
    get_project_budget,
    get_project_consumption,
    init_db,
)


def cost_summary(project: str) -> dict[str, Any]:
    """Per-project GPU-hours, USD, and carbon.

    Prefers the direct ``model_costs.project`` column (the completed attribution path); also returns
    the membership-union figure (`get_project_consumption`) so callers can see both during the
    transition. Carbon is summed for the same attributed rows when the column is present.
    """
    init_db()
    with get_db() as conn:
        direct = conn.execute(
            """SELECT COALESCE(SUM(gpu_hours),0) AS gpu_hours,
                      COALESCE(SUM(cost_usd),0)  AS cost_usd,
                      COUNT(*)                   AS n
               FROM model_costs WHERE project = ?""",
            (project,),
        ).fetchone()
        # Carbon: join carbon_records to the project's attributed models (best-effort).
        carbon = 0.0
        try:
            crow = conn.execute(
                """SELECT COALESCE(SUM(cr.co2e_g), 0) AS g
                   FROM carbon_records cr
                   WHERE cr.model IN (
                       SELECT DISTINCT model_name FROM model_costs WHERE project = ?
                   )""",
                (project,),
            ).fetchone()
            carbon = float(crow["g"]) if crow and crow["g"] is not None else 0.0
        except Exception:
            carbon = 0.0
    union = get_project_consumption(project)
    return {
        "project": project,
        "gpu_hours": float(direct["gpu_hours"]),
        "cost_usd": float(direct["cost_usd"]),
        "carbon_grams_co2e": carbon,
        "records": int(direct["n"]),
        "union_gpu_hours": union["gpu_hours"],
        "union_cost_usd": union["cost_usd"],
    }


def budget_status(project: str, *, actor: str | None = None, audit: bool = False) -> dict[str, Any]:
    """Compare a project's consumption against its budget and GPU quota; flag breaches.

    Returns ``{project, consumption, budget, quota, breaches: [...], over_budget: bool}``. When
    ``audit`` is set and there is a breach, writes a governance audit event.
    """
    proj = get_project(project)
    summary = cost_summary(project)
    budget = get_project_budget(project)
    breaches: list[str] = []
    if budget:
        gpu_b = budget.get("gpu_hours_budget")
        cost_b = budget.get("cost_budget")
        if gpu_b is not None and summary["gpu_hours"] > gpu_b:
            breaches.append(f"GPU-hours {summary['gpu_hours']:.1f} exceed budget {gpu_b:.1f}")
        if cost_b is not None and summary["cost_usd"] > cost_b:
            breaches.append(f"cost ${summary['cost_usd']:.2f} exceeds budget ${cost_b:.2f}")
    result = {
        "project": project,
        "consumption": {"gpu_hours": summary["gpu_hours"], "cost_usd": summary["cost_usd"]},
        "budget": budget,
        "quota": (
            {"gpu_limit": proj["gpu_limit"], "cpu_limit": proj["cpu_limit"]} if proj else None
        ),
        "breaches": breaches,
        "over_budget": bool(breaches),
    }
    if audit and breaches:
        from examlops.platform_db import write_audit_event

        write_audit_event(
            "exa-finops", actor, "project_budget_breach", project, {"breaches": breaches}
        )
    return result
