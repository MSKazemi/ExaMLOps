"""E3 — `exa hpc gpu-share`: fractional GPU allocation & bin-packing (ADR 0030).

Plan a fractional/MIG GPU allocation with capability-aware mechanism selection (honest
fallback when fractional sharing isn't supported), bin-pack fractional asks onto whole
GPUs, and report fractional GPU-hour accounting.
"""

from __future__ import annotations

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Fractional GPU sharing — MIG / time-slice allocation & bin-packing",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa hpc gpu-share plan JPCP --fraction 0.25 --mig 2g.10gb --mig-capable\n\n"
    "  exa hpc gpu-share plan JPCP --fraction 0.5   # no fractional support => honest fallback\n\n"
    "  exa hpc gpu-share plan JPCP --fraction 0.25 --cluster gpu-cluster --scheduler slurm\n\n"
    "  exa hpc gpu-share pack --ask a:0.5 --ask b:0.3 --ask c:0.4 --gpus 2 --timeslice\n\n"
    "  exa hpc gpu-share accounting"
)


def _caps(mig_capable: bool, timeslice: bool):
    from examlops.gpu_sharing import ClusterGpuCaps

    return ClusterGpuCaps(
        supports_mig=mig_capable,
        supports_timeslice=timeslice,
        mig_profiles=["1g.5gb", "2g.10gb", "3g.20gb", "7g.40gb"] if mig_capable else [],
    )


def _cluster_caps_and_scheduler(cluster: str):
    """Registered cluster -> (ClusterGpuCaps, scheduler). Refuses a non-ACTIVE cluster."""
    import json

    from examlops.gpu_sharing import caps_from_capabilities
    from examlops.hpc_registry import ClusterNotActiveError, require_active

    try:
        merged = require_active(cluster)
    except ClusterNotActiveError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    caps = merged.get("capabilities")
    if isinstance(caps, str):
        try:
            caps = json.loads(caps) if caps else None
        except ValueError:
            caps = None
    scheduler = str(merged.get("scheduler") or "mock")
    return caps_from_capabilities(caps), ("mock" if scheduler == "unmanaged" else scheduler)


@app.command("plan", epilog=_EXAMPLES)
def plan(
    model: str = typer.Argument(..., help="Model / workload label"),
    fraction: float = typer.Option(1.0, "--fraction", help="GPU fraction requested (0..1)"),
    mig: str = typer.Option(None, "--mig", help="MIG profile (e.g. 2g.10gb)"),
    mig_capable: bool = typer.Option(False, "--mig-capable", help="Cluster supports MIG"),
    timeslice: bool = typer.Option(False, "--timeslice", help="Cluster supports time-slicing"),
    record: bool = typer.Option(False, "--record", help="Persist the allocation"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
    cluster: str = typer.Option(
        None,
        "--cluster",
        help="Use a registered ACTIVE cluster's declared GPU-sharing capabilities and scheduler "
        "(overrides --mig-capable/--timeslice)",
    ),
    scheduler: str = typer.Option(
        None,
        "--scheduler",
        help="Also show the resources this maps to on slurm|flux|mock (ADR 0030 decision 3)",
    ),
    gpus: int = typer.Option(1, "--gpus", help="GPU devices the ask spans (with --scheduler)"),
) -> None:
    """Select the best GPU-sharing mechanism for a request (honest fallback)."""
    from examlops.gpu_sharing import FractionalAsk, record_allocation, select_mechanism
    from examlops.gpu_sharing.scheduler_map import GpuSharingError, map_to_scheduler

    if cluster:
        caps, cluster_scheduler = _cluster_caps_and_scheduler(cluster)
        scheduler = scheduler or cluster_scheduler
    else:
        caps = _caps(mig_capable, timeslice)
    ask = FractionalAsk(model, fraction=fraction, mig_profile=mig)
    choice = select_mechanism(ask, caps)
    mapping = None
    if scheduler:
        try:
            mapping = map_to_scheduler(scheduler, {}, ask, caps, gpus=gpus)
        except GpuSharingError as exc:
            _output.error(str(exc))
            raise typer.Exit(1) from exc
        choice = mapping.choice  # the scheduler may only express a coarser allocation
    if record:
        record_allocation(model, choice, tenant=tenant, scheduler=scheduler)
    if _output.json_mode:
        payload = choice.as_dict()
        if mapping is not None:
            payload["scheduler"] = mapping.scheduler
            payload["resources"] = mapping.resources
            payload["warnings"] = mapping.warnings
        _output.print_json(payload)
        return
    _output.info(
        f"{model}: {choice.mechanism} "
        f"(isolation: {choice.isolation}) — {choice.allocated_fraction:.2f} GPU"
    )
    _output.info(f"  {choice.note}")
    if mapping is not None:
        flags = " ".join(f"{k}={v}" for k, v in sorted(mapping.resources.items()))
        _output.info(f"  {mapping.scheduler} resources: {flags or '(none)'}")
    if choice.wasted_fraction > 0.01:
        _output.warning(
            f"  {choice.wasted_fraction * 100:.0f}% GPU capacity wasted by this "
            f"{'fallback' if choice.mechanism == 'whole' else 'allocation (rounded up)'}."
        )


@app.command("pack")
def pack(
    ask: list[str] = typer.Option(..., "--ask", help="label:fraction (repeatable)"),
    gpus: int = typer.Option(1, "--gpus", help="Number of whole GPUs available"),
    mig_capable: bool = typer.Option(False, "--mig-capable", help="Cluster supports MIG"),
    timeslice: bool = typer.Option(False, "--timeslice", help="Cluster supports time-slicing"),
) -> None:
    """Bin-pack fractional asks onto whole GPUs (first-fit-decreasing)."""
    from examlops.gpu_sharing import FractionalAsk, bin_pack

    asks = []
    for a in ask:
        label, _, frac = a.partition(":")
        try:
            asks.append(FractionalAsk(label, fraction=float(frac or "1.0")))
        except ValueError:
            _output.error(f"Invalid --ask '{a}' (expected label:fraction)")
            return
    result = bin_pack(asks, gpus, _caps(mig_capable, timeslice))
    if _output.json_mode:
        _output.print_json(
            {
                "gpus_used": result.gpus_used,
                "placements": [
                    {
                        "ask": p.ask_label,
                        "gpu": p.gpu_index,
                        "fraction": p.fraction,
                        "mechanism": p.mechanism,
                        "isolation": p.isolation,
                    }
                    for p in result.placements
                ],
                "unplaced": result.unplaced,
            }
        )
        return
    _output.print_table(
        f"GPU Bin-Packing — {result.gpus_used}/{gpus} GPU(s) used",
        ["Ask", "GPU", "Fraction", "Mechanism", "Isolation"],
        [
            [p.ask_label, str(p.gpu_index), f"{p.fraction:.2f}", p.mechanism, p.isolation]
            for p in result.placements
        ],
    )
    if result.unplaced:
        _output.warning(f"Unplaced (insufficient capacity): {', '.join(result.unplaced)}")


@app.command("accounting")
def accounting(
    tenant: str = typer.Option(None, "--tenant", help="Filter to one tenant"),
) -> None:
    """Show recorded fractional GPU allocations."""
    from examlops.gpu_sharing import list_allocations

    rows = list_allocations(tenant=tenant)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No GPU allocations recorded yet.")
        return
    _output.print_table(
        "GPU Allocations",
        ["Time", "Model", "Mechanism", "Fraction", "Isolation"],
        [
            [r["ts"], r["model"], r["mechanism"], f"{r['fraction']:.2f}", r["isolation"]]
            for r in rows
        ],
    )
