"""``exa fleet`` — Fleet Digital Twin & What-If Studio (Phase 5 item 5.1)."""

from __future__ import annotations

import typer

from examlops.cli import _output

app = typer.Typer(
    no_args_is_help=True, help="Fleet Digital Twin — what-if simulation over the live fleet"
)

_EX = (
    "Examples:\n\n"
    "  [dim]# What happens if we submit 20 two-GPU jobs right now?[/dim]\n"
    "  exa fleet simulate --jobs 20 --gpus 2\n\n"
    "  [dim]# ...and add 8 GPUs to cluster lxp, carbon-aware placement[/dim]\n"
    "  exa fleet simulate --jobs 20 --gpus 2 --add-gpus lxp=8 --optimize carbon-aware"
)


@app.command("simulate", epilog=_EX)
def simulate(
    jobs: int = typer.Option(0, "--jobs", "-j", help="Number of jobs to submit in the scenario"),
    gpus: int = typer.Option(1, "--gpus", "-g", help="GPUs per job"),
    nodes: int = typer.Option(1, "--nodes", "-N", help="Nodes per job"),
    duration_h: float = typer.Option(1.0, "--duration", help="Hours per job (for cost/carbon)"),
    add_gpus: list[str] = typer.Option(
        None, "--add-gpus", help="Add idle GPUs, e.g. --add-gpus lxp=8 (repeatable)"
    ),
    carbon: list[str] = typer.Option(
        None, "--carbon", help="Override grid carbon, e.g. --carbon lxp=600 (repeatable)"
    ),
    optimize: str | None = typer.Option(
        None,
        "--optimize",
        help="Placement provider: least-loaded|carbon-aware|cost-aware|carbon-cost-balanced",
    ),
) -> None:
    """Project a hypothetical scenario over the live fleet — placements, GPU-hours, cost, carbon, queue."""
    from examlops.fleet_twin import JobSpec, Scenario
    from examlops.fleet_twin import simulate as _simulate
    from examlops.hpc_placement_providers import resolve_placement_score_fn

    def _kv(pairs: list[str] | None, cast) -> dict:
        out: dict = {}
        for p in pairs or []:
            if "=" in p:
                k, v = p.split("=", 1)
                try:
                    out[k.strip()] = cast(v)
                except ValueError:
                    _output.warning(f"ignoring bad --value '{p}'")
        return out

    scenario = Scenario(
        add_gpus=_kv(add_gpus, int),
        carbon_overrides=_kv(carbon, float),
        jobs=[JobSpec(gpus=gpus, nodes=nodes, duration_h=duration_h, count=jobs)] if jobs else [],
    )
    score_fn = resolve_placement_score_fn(optimize) if optimize else None
    result = _simulate(scenario, score_fn=score_fn)

    if _output.json_mode:
        _output.print_json(result)
        return
    p = result["projected"]
    d = result["delta"]
    _output.print_record(
        {
            "placed": p["placed"],
            "queued": p["queued"],
            "projected_gpu_hours": p["projected_gpu_hours"],
            "projected_cost_usd": p["projected_cost_usd"],
            "projected_carbon_kg": p["projected_carbon_kg"],
            "Δ cost_usd vs baseline": d["cost_usd"],
            "Δ carbon_kg vs baseline": d["carbon_kg"],
            "Δ queue_depth vs baseline": d["queue_depth"],
        }
    )
    if p["placements"]:
        _output.print_table(
            "Projected placements",
            ["Job", "Cluster", "GPUs"],
            [[str(x["job"]), x["cluster"], str(x["gpus"])] for x in p["placements"][:50]],
        )


@app.command("heatmap")
def heatmap(
    cluster: str = typer.Option(None, "--cluster", "-c", help="Limit to one cluster"),
    cols: int = typer.Option(None, "--cols", help="Grid width override"),
) -> None:
    """Server-side tile grid for the 3D/NOC fleet heatmap (item 5.4); JSON the 3D view renders."""
    from examlops.fleetscape import fleet_heatmap

    grid = fleet_heatmap(cluster, cols=cols)
    if _output.json_mode:
        _output.print_json(grid)
        return
    s = grid["summary"]
    health = s["fleet_health"]
    _output.print_record(
        {
            "nodes": s["nodes"],
            "total_gpus": s["total_gpus"],
            "down_nodes": s["down_nodes"],
            # An empty fleet has no health to average. Printing a number here — any number —
            # would be read as a measurement of a fleet that was never looked at.
            "fleet_health": "— (no nodes)" if health is None else health,
            "grid": f"{grid['dims']['rows']}×{grid['dims']['cols']}",
        }
    )
    if health is None:
        _output.warning(
            "No nodes in the registry for this view, so fleet health is not a measurement. "
            "Run: exa hpc detect"
        )
