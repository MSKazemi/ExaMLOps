"""FinOps / Green-AI read tools — close the gap where the finops specialist had no cost data.

The router's ``finops`` pack (cost/spend/budget/carbon triggers) previously held only
``get_metrics`` + ``get_platform_summary``, neither of which reads the FinOps tables, so a cost
question was answered with "no data". These tools read the same ``platform.db`` tables the
``exa models cost`` / ``exa finops`` / ``exa project budget`` commands use. All read-only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from langchain_core.tools import tool

# Allow importing the examlops CLI package (same shim as platform_ops.py).
_CLI_SRC = Path(__file__).resolve().parents[4] / "cli" / "src"
if str(_CLI_SRC) not in sys.path:
    sys.path.insert(0, str(_CLI_SRC))

try:
    from examlops.platform_db import (
        aggregate_model_costs,
        get_carbon_records,
        get_model_costs,
        get_project_budget,
        get_project_consumption,
        init_db,
        list_project_budgets,
        total_gateway_cost,
    )

    init_db()
    _DB_OK = True
except Exception:
    _DB_OK = False

_DB_UNAVAILABLE = "platform.db not available — set PLATFORM_DB and ensure the CLI is installed"


@tool
def get_cost_summary() -> str:
    """Per-model GPU-hour and USD cost rollup across all recorded training runs.

    Use this first for any 'what did we spend / which model costs the most?' question.
    """
    if not _DB_OK:
        return _DB_UNAVAILABLE
    rows = aggregate_model_costs()
    if not rows:
        return "No cost records yet — record one with `exa models cost <model> --record`."
    gateway = total_gateway_cost()
    out = {"models": rows, "gateway_llm_cost_usd": round(gateway, 4)}
    return json.dumps(out, default=str, indent=2)


@tool
def get_model_cost_history(model_name: str) -> str:
    """Cost history (per training run: GPU-hours, USD, HPC job id) for one model.

    Args:
        model_name: Registered model name (e.g. 'jpcp').
    """
    if not _DB_OK:
        return _DB_UNAVAILABLE
    rows = get_model_costs(model_name)
    if not rows:
        return f"No cost records for {model_name!r}."
    return json.dumps(rows, default=str, indent=2)


@tool
def get_carbon_summary(model_name: str = "") -> str:
    """Green-AI carbon records (kgCO2e per run, with provider/methodology), newest first.

    Args:
        model_name: Optional model filter; empty returns all models.
    """
    if not _DB_OK:
        return _DB_UNAVAILABLE
    rows = get_carbon_records(model_name or None)
    if not rows:
        return "No carbon records yet — record one with `exa finops carbon`."
    total_g = sum(float(r.get("co2e_g") or 0) for r in rows)
    return json.dumps(
        {"total_kg_co2e": round(total_g / 1000.0, 4), "records": rows[:50]},
        default=str,
        indent=2,
    )


@tool
def get_budget_status(project: str = "") -> str:
    """Project budgets vs actual consumption — flags any budget breach.

    Args:
        project: Optional project name; empty returns every project with a budget.
    """
    if not _DB_OK:
        return _DB_UNAVAILABLE
    budgets = [b for b in [get_project_budget(project)] if b] if project else list_project_budgets()
    if not budgets:
        return "No project budgets set — set one with `exa project budget <project> --usd N`."
    out = []
    for b in budgets:
        name = b.get("project", project)
        consumption = get_project_consumption(name)
        cost_limit = float(b.get("cost_budget") or 0)
        gpu_limit = float(b.get("gpu_hours_budget") or 0)
        cost_breach = bool(cost_limit and float(consumption["cost_usd"]) > cost_limit)
        gpu_breach = bool(gpu_limit and float(consumption["gpu_hours"]) > gpu_limit)
        out.append(
            {
                "project": name,
                "budget": b,
                "consumption": consumption,
                "breached": cost_breach or gpu_breach,
            }
        )
    return json.dumps(out, default=str, indent=2)


TOOLS = [get_cost_summary, get_model_cost_history, get_carbon_summary, get_budget_status]
