"""P4 — Project FinOps & monitoring attribution (ADR 0089).

Completes the deferred per-project cost attribution (finops.py:102): sums cost/carbon directly by
the ``model_costs.project`` column (populated by P1/P3), and compares consumption against the
project's budget and GPU quota to flag breaches. Read-only aggregation over ``platform.db`` — no
schema changes; degrades cleanly when nothing is recorded yet.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from examlops.data import get_db, init_db
from examlops.data.projects import get_project, get_project_budget, get_project_consumption


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


#: Budget periods a budget may name. A period is a *window*, and the consumption a budget is
#: compared against has to be the spend inside it.
PERIODS = ("monthly", "total")


def period_window(period: str | None, *, now: datetime | None = None) -> tuple[str | None, str]:
    """``(since, period)`` for a budget period: the ISO-8601 UTC start of its window.

    ``monthly`` (the default a budget is written with) starts at the first instant of the current
    calendar month, UTC; ``total`` has no start and means lifetime spend. An unrecognised period is
    treated as monthly — the default — and the caller reports which period was used, because a
    budget compared against the wrong window is a number that does not mean what it says.
    """
    resolved = (period or "monthly").strip().lower()
    if resolved not in PERIODS:
        resolved = "monthly"
    if resolved == "total":
        return None, resolved
    current = now or datetime.now(UTC)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(timespec="seconds"), resolved


def budget_status(
    project: str,
    *,
    actor: str | None = None,
    audit: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compare a project's consumption against its budget and GPU quota; flag breaches.

    The consumption compared is the spend **inside the budget's period** (ADR 0089): a budget row
    carries one (``monthly`` by default) and every cost ever recorded used to be summed against it,
    so a monthly budget breached permanently once lifetime spend passed it and never reset. Lifetime
    numbers are still reported as ``consumption_total``.

    Returns ``{project, period, window_start, consumption, consumption_total, budget, quota,
    breaches, over_budget}``. ``audit`` writes a governance event when there is a breach; prefer
    :func:`evaluate_budget`, which raises one on the *transition* instead of on every look.
    """
    proj = get_project(project)
    budget = get_project_budget(project)
    since, period = period_window((budget or {}).get("period"), now=now)
    windowed = (
        get_project_consumption(project, since) if since else get_project_consumption(project)
    )
    lifetime = get_project_consumption(project)
    breaches: list[str] = []
    if budget:
        gpu_b = budget.get("gpu_hours_budget")
        cost_b = budget.get("cost_budget")
        if gpu_b is not None and windowed["gpu_hours"] > gpu_b:
            breaches.append(f"GPU-hours {windowed['gpu_hours']:.1f} exceed budget {gpu_b:.1f}")
        if cost_b is not None and windowed["cost_usd"] > cost_b:
            breaches.append(f"cost ${windowed['cost_usd']:.2f} exceeds budget ${cost_b:.2f}")
    result = {
        "project": project,
        "period": period,
        "window_start": since,
        "consumption": {"gpu_hours": windowed["gpu_hours"], "cost_usd": windowed["cost_usd"]},
        "consumption_total": {"gpu_hours": lifetime["gpu_hours"], "cost_usd": lifetime["cost_usd"]},
        "budget": budget,
        "quota": (
            {"gpu_limit": proj["gpu_limit"], "cpu_limit": proj["cpu_limit"]} if proj else None
        ),
        "breaches": breaches,
        "over_budget": bool(breaches),
    }
    if audit and breaches:
        from examlops.data.audit import write_audit_event

        write_audit_event(
            "exa-finops", actor, "project_budget_breach", project, {"breaches": breaches}
        )
    return result


def budget_engine_gate(
    project: str, *, requested_gpu_hours: float = 0.0, tenant: str = "default"
) -> Any:
    """Consult the policy engine's ``budget`` gate for ``project`` (ADR 0029 decision 3).

    Returns ``None`` when the gate is off (the default — nothing is checked, nothing audited) and an
    ``EngineDecision`` otherwise. A project with no GPU-hour budget is never over one. The check is
    *consumption in the budget's period + the request* against the budget, GPU-hours only: the cost
    (USD) dimension of a budget is still reported by :func:`budget_status`, not gated here.
    """
    from examlops.policy_engine import EngineDecision, budget_gate
    from examlops.policy_engine.gates import consult

    def _evaluate(_opts: dict[str, Any]) -> Any:
        status = budget_status(project)
        limit = (status["budget"] or {}).get("gpu_hours_budget")
        if limit is None:
            return EngineDecision(True, [f"{project} has no GPU-hour budget"], "allow", "builtin")
        used = float(status["consumption"]["gpu_hours"]) + float(requested_gpu_hours)
        return budget_gate(used, float(limit), tenant=tenant)

    return consult("budget", _evaluate)


def evaluate_budget(
    project: str, *, actor: str | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Evaluate a project's budget and announce a **change** in its state (ADR 0089).

    The clause says a breach "raises a governance event". Nothing did so on its own: the event was
    written by `exa project budget`, so a breach existed only while somebody was looking — and once
    per look, so a breached project collected one duplicate event per run. This is the automatic
    point: it writes ``project_budget_breach`` when a project enters breach (or the breach list
    changes) and ``project_budget_recovered`` when it leaves, and nothing while the state holds.

    Returns the status with ``alert`` — ``breached`` / ``recovered`` / ``None`` (unchanged).
    """
    from examlops.data.audit import write_audit_event
    from examlops.data.projects import get_project_budget_alert, set_project_budget_alert

    status = budget_status(project, now=now)
    if status["budget"] is None:  # nothing to be over
        return {**status, "alert": None}

    previous = get_project_budget_alert(project) or {"state": "ok", "breaches": []}
    breaches = status["breaches"]
    state = "breached" if breaches else "ok"
    changed = state != previous["state"] or (
        state == "breached" and breaches != previous["breaches"]
    )
    if not changed:
        return {**status, "alert": None}

    alert = "breached" if state == "breached" else "recovered"
    # Spelled out, not derived from `alert`: these two strings are the governance record's own
    # vocabulary (ADR 0089) and a rename would silently orphan every event already written.
    action = "project_budget_breach" if state == "breached" else "project_budget_recovered"
    write_audit_event(
        "exa-finops",
        actor,
        action,
        project,
        {"breaches": breaches, "period": status["period"], "was": previous["breaches"]},
    )
    set_project_budget_alert(project, state, breaches)
    return {**status, "alert": alert}
