from __future__ import annotations

import os

import typer

from examlops.cli import _output
from examlops.data import init_db
from examlops.data.audit import write_audit_event
from examlops.data.finops import get_carbon_records, write_carbon_record
from examlops.data.projects import (
    get_project_budget,
    get_project_consumption,
    list_project_budgets,
    set_project_budget,
)
from examlops.finops.carbon import (
    DEFAULT_GRID_INTENSITY_G_PER_KWH,
    budget_usage_ratio,
    estimate_carbon_via_provider,
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
cost_app = typer.Typer(
    help="HPC cost providers (pluggable rate cards). Estimation runs via 'exa models cost'.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(budget_app, name="budget")
app.add_typer(carbon_app, name="carbon")
app.add_typer(cost_app, name="cost")

_EX_BUDGET_SET = (
    "Examples:\n\n"
    "  exa finops budget set eu-hpc --gpu-hours 1000 --cost 5000\n\n"
    "  exa finops budget set eu-hpc --gpu-hours 500 --period weekly"
)
_EX_BUDGET_STATUS = "Examples:\n\n  exa finops budget status\n\n  exa finops budget status eu-hpc"
_EX_CARBON_EST = (
    "Examples:\n\n"
    "  exa finops carbon estimate --gpu-hours 12\n\n"
    "  exa finops carbon estimate --gpu-hours 12 --grid-intensity 232\n\n"
    "  exa finops carbon estimate --gpu-hours 12 --provider ccf-like --pue 1.3"
)
_EX_CARBON_RECORD = (
    "Examples:\n\n"
    "  exa finops carbon record JPCP --gpu-hours 12\n\n"
    "  exa finops carbon record JPCP --gpu-hours 12 --run-id abc123 --grid-intensity 232\n\n"
    "  exa finops carbon record JPCP --gpu-hours 12 --provider codecarbon-like"
)
_EX_CARBON_REPORT = (
    "Examples:\n\n  exa finops carbon report\n\n  exa finops carbon report --model JPCP"
)
_EX_CARBON_PROVIDERS = (
    "Examples:\n\n"
    "  exa finops carbon providers\n\n"
    "  exa finops carbon providers --json\n\n"
    "Add your own: ship a plugin under the 'exa.providers.carbon' entry-point group, or set a\n"
    "declarative formula in ~/.config/examlops/finops.yaml (provider: expression). See\n"
    "docs/guides/finops-providers.md."
)
_EX_CARBON_SIGNAL = (
    "Examples:\n\n"
    "  exa finops carbon signal\n\n"
    "  exa --json finops carbon signal\n\n"
    "Signal type is derived from EXAMLOPS_GRID_INTENSITY_METHOD, never declared separately —\n"
    "an average feed labelled 'decision' is the exact error ADR 0112 exists to prevent."
)

_EX_COST_PROVIDERS = (
    "Examples:\n\n"
    "  exa finops cost providers\n\n"
    "Cost estimation itself runs inside 'exa models cost --record'. Select a rate card with the\n"
    "[finops.cost] block in ~/.config/examlops/finops.yaml or EXAMLOPS_COST_PROVIDER, or ship a\n"
    "plugin under the 'exa.providers.cost' entry-point group. See docs/guides/finops-providers.md."
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
    _output.ok(f"Budget set for {project} (period: {period}).")


def _status(project: str) -> dict:
    """Budget vs consumption for one project, as numbers (the JSON shape; the table formats it)."""
    budget = get_project_budget(project) or {}
    consumed = get_project_consumption(project)
    gpu_budget = budget.get("gpu_hours_budget")
    cost_budget = budget.get("cost_budget")
    gpu_ratio = budget_usage_ratio(consumed["gpu_hours"], gpu_budget)
    cost_ratio = budget_usage_ratio(consumed["cost_usd"], cost_budget)
    over = (gpu_ratio is not None and gpu_ratio > 1.0) or (
        cost_ratio is not None and cost_ratio > 1.0
    )
    return {
        "project": project,
        "status": "OVER" if over else "ok",
        "gpu_hours_used": round(float(consumed["gpu_hours"]), 3),
        "gpu_hours_budget": gpu_budget,
        "gpu_pct": None if gpu_ratio is None else round(gpu_ratio * 100, 1),
        "cost_used_usd": round(float(consumed["cost_usd"]), 2),
        "cost_budget_usd": cost_budget,
        "cost_pct": None if cost_ratio is None else round(cost_ratio * 100, 1),
    }


def _status_row(project: str) -> list:
    st = _status(project)

    def _pct(value: float | None) -> str:
        return "—" if value is None else f"{value:.0f}%"

    return [
        project,
        f"{st['gpu_hours_used']:.1f} / {st['gpu_hours_budget'] or '—'}",
        _pct(st["gpu_pct"]),
        f"{st['cost_used_usd']:.0f} / {st['cost_budget_usd'] or '—'}",
        _pct(st["cost_pct"]),
        st["status"],
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
    if _output.json_mode:
        # Numbers with honest names. This used to emit the table's display strings under
        # misleading keys — `gpu_used` was a percentage, `cost` read "0 / —".
        _output.print_json([_status(p) for p in projects])
        return
    _output.print_table(
        "Project Budgets",
        ["Project", "GPU-h used/budget", "GPU%", "Cost used/budget", "Cost%", "Status"],
        [_status_row(p) for p in projects],
    )


def _carbon_overrides(grid_intensity: float, pue: float | None, gpu_tdp: float | None) -> dict:
    """Build the per-call coefficient overrides (only for flags the user set)."""
    overrides: dict[str, float] = {"grid_intensity_g_per_kwh": grid_intensity}
    if pue is not None:
        overrides["pue"] = pue
    if gpu_tdp is not None:
        overrides["gpu_tdp_watts"] = gpu_tdp
    return overrides


def _reporting_signal(grid_intensity: float):
    """The typed signal behind a reported figure (ADR 0112 decision 6).

    An explicit ``--grid-intensity`` is ``operator_supplied``; the untouched default is
    ``static_default``. Both are *accounting* signals, which is what a report needs — and
    naming which one it is stops a documented constant being read as a measurement.
    """
    from examlops.finops.carbon_signal import CarbonSignal
    from examlops.finops.grid_intensity import STATIC_METHOD

    method = (
        STATIC_METHOD if grid_intensity == DEFAULT_GRID_INTENSITY_G_PER_KWH else "operator_supplied"
    )
    return CarbonSignal(grams_per_kwh=grid_intensity, method=method, source="cli")


def _estimate_or_exit(
    gpu_hours: float,
    cpu_hours: float,
    provider: str | None,
    grid_intensity: float,
    pue: float | None,
    gpu_tdp: float | None,
) -> dict:
    """Estimate, or exit 1 with the reason — never fall back to a smaller number.

    Two ways to end up publishing a figure that means nothing, both refused here: asking about no
    work at all, and handing CPU-hours to a provider that has no term for them.
    """
    from examlops.finops.carbon import CarbonInputUnaccounted

    if gpu_hours <= 0 and cpu_hours <= 0:
        _output.error(
            "nothing to account for: no GPU-hours and no CPU-hours",
            hint=(
                "pass --gpu-hours and/or --cpu-hours. A record of 0 kWh is a claim that the run "
                "consumed no energy, not a note that nobody counted it."
            ),
        )
    try:
        return estimate_carbon_via_provider(
            gpu_hours,
            cpu_hours=cpu_hours,
            provider=provider,
            **_carbon_overrides(grid_intensity, pue, gpu_tdp),
        )
    except CarbonInputUnaccounted as exc:
        _output.error(str(exc))
        raise  # unreachable: _output.error exits — keeps the type checker and the reader honest


@carbon_app.command("estimate", epilog=_EX_CARBON_EST)
def carbon_estimate(
    gpu_hours: float = typer.Option(0.0, "--gpu-hours", help="GPU-hours to estimate"),
    cpu_hours: float = typer.Option(
        0.0, "--cpu-hours", help="CPU-core-hours to estimate (a CPU-only run is not zero-carbon)"
    ),
    grid_intensity: float = typer.Option(
        DEFAULT_GRID_INTENSITY_G_PER_KWH, "--grid-intensity", help="gCO2e per kWh"
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Carbon provider (default: green-ai-default). See: carbon providers",
    ),
    pue: float | None = typer.Option(None, "--pue", help="Override datacentre PUE"),
    gpu_tdp: float | None = typer.Option(None, "--gpu-tdp", help="Override GPU TDP (watts)"),
) -> None:
    """Estimate energy (kWh) and CO2e (g) for GPU-hours and CPU-core-hours (no DB write).

    Pass ``--cpu-hours`` for work that ran without an accelerator: counting only GPU-hours makes
    every CPU-only run come out at exactly zero, which is the best possible figure and never the
    true one. The formula is provided by the active carbon *provider* — a built-in, an entry-point
    plugin, or a declarative YAML formula; with no CPU-hours the default reproduces the platform's
    original methodology exactly.
    """
    est = _estimate_or_exit(gpu_hours, cpu_hours, provider, grid_intensity, pue, gpu_tdp)
    signal = _reporting_signal(grid_intensity)
    if _output.json_mode:
        _output.print_json(
            {"gpu_hours": gpu_hours, "cpu_hours": cpu_hours, **est, "signal": signal.as_dict()}
        )
        return
    _output.print_table(
        f"Carbon estimate — {gpu_hours} GPU-h, {cpu_hours} CPU-core-h",
        ["Metric", "Value"],
        [
            ["Energy (kWh)", f"{est['kwh']:.3f}"],
            ["CO2e (g)", f"{est['co2e_g']:.1f}"],
            ["Provider", est["provider"]],
            [
                "Uncertainty",
                "—" if est["uncertainty"] is None else f"±{est['uncertainty'] * 100:.0f}%",
            ],
            # ADR 0112 decision 6 — a carbon figure that does not say what kind of signal
            # produced it can be quoted on a path where that kind is the wrong one.
            ["Signal type", signal.signal_type],
            ["Method", signal.method],
        ],
    )


@carbon_app.command("record", epilog=_EX_CARBON_RECORD)
def carbon_record(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    gpu_hours: float = typer.Option(0.0, "--gpu-hours", help="GPU-hours consumed by the run"),
    cpu_hours: float = typer.Option(
        0.0, "--cpu-hours", help="CPU-core-hours consumed by the run (counted, not assumed zero)"
    ),
    run_id: str | None = typer.Option(None, "--run-id", help="MLflow run id"),
    grid_intensity: float = typer.Option(
        DEFAULT_GRID_INTENSITY_G_PER_KWH, "--grid-intensity", help="gCO2e per kWh"
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Carbon provider (default: green-ai-default). See: carbon providers",
    ),
    pue: float | None = typer.Option(None, "--pue", help="Override datacentre PUE"),
    gpu_tdp: float | None = typer.Option(None, "--gpu-tdp", help="Override GPU TDP (watts)"),
) -> None:
    """Estimate (via the active provider) and persist a carbon record for a training run."""
    init_db()
    est = _estimate_or_exit(gpu_hours, cpu_hours, provider, grid_intensity, pue, gpu_tdp)
    signal = _reporting_signal(grid_intensity)
    write_carbon_record(
        model,
        run_id,
        est["kwh"],
        est["co2e_g"],
        grid_intensity,
        est["provider"],
        signal_type=signal.signal_type,
        signal_method=signal.method,
    )
    write_audit_event(
        "cli",
        _actor(),
        "carbon_recorded",
        model,
        {
            "gpu_hours": gpu_hours,
            "cpu_hours": cpu_hours,
            "provider": est["provider"],
            "kwh": est["kwh"],
            "co2e_g": est["co2e_g"],
            **signal.as_dict(),
        },
    )
    _output.ok(
        f"Recorded {est['kwh']:.3f} kWh / {est['co2e_g']:.1f} gCO2e for {model} "
        f"(provider: {est['provider']})."
    )


def _list_providers(domain: str, register_module: str, title: str) -> None:
    """Shared renderer for `<domain> providers` — built-ins + entry-point plugins + status."""
    import importlib

    importlib.import_module(register_module)  # registers the built-ins as a side effect
    from examlops.providers import default_provider_name, list_providers

    infos = list_providers(domain)
    default = default_provider_name(domain)
    if _output.json_mode:
        _output.print_json(
            [
                {
                    "name": i.name,
                    "kind": i.kind,
                    "default": i.name == default,
                    "ok": i.ok,
                    "methodology": (
                        i.provider.metadata().methodology if i.ok and i.provider else None
                    ),
                    "uncertainty": (
                        i.provider.metadata().uncertainty if i.ok and i.provider else None
                    ),
                    "error": i.error,
                }
                for i in infos
            ]
        )
        return
    rows = []
    for i in infos:
        meta = i.provider.metadata() if i.ok and i.provider else None
        unc = "—" if not meta or meta.uncertainty is None else f"±{meta.uncertainty * 100:.0f}%"
        status = "ok" if i.ok else f"ERROR: {i.error}"
        rows.append([i.name + ("  (default)" if i.name == default else ""), i.kind, unc, status])
    _output.print_table(title, ["Name", "Kind", "Uncertainty", "Status"], rows)


@carbon_app.command("providers", epilog=_EX_CARBON_PROVIDERS)
def carbon_providers() -> None:
    """List the available carbon providers (built-ins + entry-point plugins) and their status."""
    _list_providers("carbon", "examlops.finops.carbon_providers", "Carbon providers")


@carbon_app.command("signal", epilog=_EX_CARBON_SIGNAL)
def carbon_signal_cmd() -> None:
    """Show the live carbon signal, its type, and what it may be used for (ADR 0112).

    Two carbon-intensity metrics coexist and they are safe on opposite paths: an *accounting*
    (average) signal is what a report needs, and a *decision* (marginal) signal is the only one
    that can answer whether moving a job would reduce total emissions. Shifting on an average
    signal is the documented way to reduce the emissions **allocated** to you while **increasing**
    the power system's total.

    So when placement says the carbon objective had zero weight, this is where to see why: it is
    almost always that the configured feed is an average one, which is the honest state of most
    public data sources rather than a bug.
    """
    from examlops.finops.carbon import DEFAULT_GRID_INTENSITY_G_PER_KWH as _default
    from examlops.finops.grid_intensity import current_grid_signal
    from examlops.hpc_placement import carbon_objective_state

    signal = current_grid_signal(_default)
    usable, reason = carbon_objective_state(signal)
    payload = {
        **signal.as_dict(),
        "usable_for_placement": usable,
        "placement_reason": reason,
        "usable_for_reporting": signal.is_accounting,
    }
    if _output.json_mode:
        _output.print_json(payload)
        return
    _output.print_table(
        "Carbon signal",
        ["Field", "Value"],
        [
            ["Intensity (gCO2e/kWh)", f"{signal.grams_per_kwh:.1f}"],
            ["Signal type", signal.signal_type],
            ["Method", signal.method],
            ["Zone", signal.zone or "—"],
            ["Source", signal.source or "—"],
            ["Usable for reporting", "yes" if signal.is_accounting else "NO"],
            ["Usable for placement", "yes" if usable else "NO"],
        ],
    )
    if not usable:
        _output.hint(
            f"Carbon cannot weigh on placement: {reason}. This is not a bug — no default is "
            "substituted, because substituting an average signal there is the harm itself. "
            "Set EXAMLOPS_GRID_INTENSITY_METHOD=locational_marginal only if the feed really "
            "is marginal."
        )


@cost_app.command("providers", epilog=_EX_COST_PROVIDERS)
def cost_providers() -> None:
    """List the available cost providers (rate cards) — built-ins + entry-point plugins."""
    _list_providers("cost", "examlops.finops.cost_providers", "Cost providers")


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
