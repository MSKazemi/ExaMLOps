from __future__ import annotations

import os

import typer

from examlops.cli import _output
from examlops.finops.carbon import (
    DEFAULT_GRID_INTENSITY_G_PER_KWH,
    budget_usage_ratio,
    estimate_carbon,
)
from examlops.platform_db import (
    get_carbon_records,
    get_project_budget,
    get_project_consumption,
    init_db,
    list_project_budgets,
    set_project_budget,
    write_audit_event,
    write_carbon_record,
)

app = typer.Typer(
    help="FinOps + Green-AI — project GPU/cost budgets and carbon accounting.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

budget_app = typer.Typer(
    help="Per-project GPU-hour / cost budgets (project = namespace).",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
carbon_app = typer.Typer(
    help="Energy (kWh) and CO2e accounting for training runs.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(budget_app, name="budget")
app.add_typer(carbon_app, name="carbon")

_EX_BUDGET_SET = (
    "Examples:\n\n"
    "  exa finops budget set eu-hpc --gpu-hours 1000 --cost 5000\n\n"
    "  exa finops budget set eu-hpc --gpu-hours 500 --period weekly"
)
_EX_BUDGET_STATUS = "Examples:\n\n  exa finops budget status\n\n  exa finops budget status eu-hpc"
_EX_CARBON_EST = (
    "Examples:\n\n"
    "  exa finops carbon estimate --gpu-hours 12\n\n"
    "  exa finops carbon estimate --gpu-hours 12 --grid-intensity 232"
)
_EX_CARBON_RECORD = (
    "Examples:\n\n"
    "  exa finops carbon record JPCP --gpu-hours 12\n\n"
    "  exa finops carbon record JPCP --gpu-hours 12 --run-id abc123 --grid-intensity 232"
)
_EX_CARBON_REPORT = (
    "Examples:\n\n  exa finops carbon report\n\n  exa finops carbon report --model JPCP"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"


@budget_app.command("set", epilog=_EX_BUDGET_SET)
def budget_set(
    project: str = typer.Argument(..., help="Project name (= namespace)"),
    gpu_hours: float | None = typer.Option(None, "--gpu-hours", help="GPU-hour budget"),
    cost: float | None = typer.Option(None, "--cost", help="Cost budget (USD)"),
    period: str = typer.Option("monthly", "--period", help="Budget period label"),
) -> None:
    """Set (or replace) a project's GPU-hour / cost budget."""
    init_db()
    if gpu_hours is None and cost is None:
        _output.error("Provide at least one of --gpu-hours or --cost.")
    set_project_budget(project, gpu_hours, cost, period=period, updated_by=_actor())
    write_audit_event(
        "cli",
        _actor(),
        "budget_set",
        project,
        {"gpu_hours": gpu_hours, "cost": cost, "period": period},
    )
    _output.ok(f"Budget set for [bold]{project}[/bold] (period: {period}).")


def _status_row(project: str) -> list:
    budget = get_project_budget(project) or {}
    consumed = get_project_consumption(project)
    gpu_ratio = budget_usage_ratio(consumed["gpu_hours"], budget.get("gpu_hours_budget"))
    cost_ratio = budget_usage_ratio(consumed["cost_usd"], budget.get("cost_budget"))
    over = (gpu_ratio is not None and gpu_ratio > 1.0) or (
        cost_ratio is not None and cost_ratio > 1.0
    )

    def _fmt(ratio: float | None) -> str:
        return "—" if ratio is None else f"{ratio * 100:.0f}%"

    return [
        project,
        f"{consumed['gpu_hours']:.1f} / {budget.get('gpu_hours_budget') or '—'}",
        _fmt(gpu_ratio),
        f"{consumed['cost_usd']:.0f} / {budget.get('cost_budget') or '—'}",
        _fmt(cost_ratio),
        "OVER" if over else "ok",
    ]


@budget_app.command("status", epilog=_EX_BUDGET_STATUS)
def budget_status(
    project: str | None = typer.Argument(None, help="Filter to one project (default: all)"),
) -> None:
    """Show budget vs recorded consumption (GPU-hours + cost) per project."""
    init_db()
    projects = [project] if project else [b["project"] for b in list_project_budgets()]
    if not projects:
        _output.info("No project budgets configured. Set one with: exa finops budget set <project>")
        return
    rows = [_status_row(p) for p in projects]
    if _output.json_mode:
        _output.print_json(
            [
                {
                    "project": r[0],
                    "gpu_hours": r[1],
                    "gpu_used": r[2],
                    "cost": r[3],
                    "cost_used": r[4],
                    "status": r[5],
                }
                for r in rows
            ]
        )
        return
    _output.print_table(
        "Project Budgets",
        ["Project", "GPU-h used/budget", "GPU%", "Cost used/budget", "Cost%", "Status"],
        rows,
    )


@carbon_app.command("estimate", epilog=_EX_CARBON_EST)
def carbon_estimate(
    gpu_hours: float = typer.Option(..., "--gpu-hours", help="GPU-hours to estimate"),
    grid_intensity: float = typer.Option(
        DEFAULT_GRID_INTENSITY_G_PER_KWH, "--grid-intensity", help="gCO2e per kWh"
    ),
) -> None:
    """Estimate energy (kWh) and CO2e (g) for a number of GPU-hours (no DB write)."""
    est = estimate_carbon(gpu_hours, grid_intensity_g_per_kwh=grid_intensity)
    _output.print_table(
        f"Carbon estimate — {gpu_hours} GPU-h",
        ["Metric", "Value"],
        [["Energy (kWh)", f"{est['kwh']:.3f}"], ["CO2e (g)", f"{est['co2e_g']:.1f}"]],
    )


@carbon_app.command("record", epilog=_EX_CARBON_RECORD)
def carbon_record(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    gpu_hours: float = typer.Option(..., "--gpu-hours", help="GPU-hours consumed by the run"),
    run_id: str | None = typer.Option(None, "--run-id", help="MLflow run id"),
    grid_intensity: float = typer.Option(
        DEFAULT_GRID_INTENSITY_G_PER_KWH, "--grid-intensity", help="gCO2e per kWh"
    ),
) -> None:
    """Estimate and persist a carbon record for a training run."""
    init_db()
    est = estimate_carbon(gpu_hours, grid_intensity_g_per_kwh=grid_intensity)
    write_carbon_record(model, run_id, est["kwh"], est["co2e_g"], grid_intensity)
    write_audit_event("cli", _actor(), "carbon_recorded", model, {"gpu_hours": gpu_hours, **est})
    _output.ok(
        f"Recorded {est['kwh']:.3f} kWh / {est['co2e_g']:.1f} gCO2e for [bold]{model}[/bold]."
    )


@carbon_app.command("report", epilog=_EX_CARBON_REPORT)
def carbon_report(
    model: str | None = typer.Option(None, "--model", "-m", help="Filter to one model"),
) -> None:
    """Aggregate recorded energy and carbon (optionally for one model)."""
    init_db()
    records = get_carbon_records(model)
    if not records:
        _output.info("No carbon records yet. Record one with: exa finops carbon record <model> ...")
        return
    total_kwh = sum(r["kwh"] or 0.0 for r in records)
    total_co2e = sum(r["co2e_g"] or 0.0 for r in records)
    if _output.json_mode:
        _output.print_json({"n": len(records), "total_kwh": total_kwh, "total_co2e_g": total_co2e})
        return
    _output.print_table(
        "Carbon Report" + (f" — {model}" if model else ""),
        ["Metric", "Value"],
        [
            ["Runs", str(len(records))],
            ["Total energy (kWh)", f"{total_kwh:.3f}"],
            ["Total CO2e (g)", f"{total_co2e:.1f}"],
            ["Total CO2e (kg)", f"{total_co2e / 1000.0:.3f}"],
        ],
    )
