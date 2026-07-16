"""E8 — `exa hardware`: heterogeneous hardware & hybrid HPC↔cloud placement (ADR 0041).

Register device pools (HPC + cloud, any accelerator), place a workload on the best-available
compatible device with honest fallback + portability rejection, plan a governed cloud burst
(residency-checked + audited), and inspect recent placement decisions.
"""

from __future__ import annotations

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Heterogeneous hardware & hybrid HPC↔cloud placement (E8)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa hardware add-pool hpc-mi300 --target hpc --accelerator amd --count 8 --region eu\n\n"
    "  exa hardware pools\n\n"
    "  exa hardware place train-llm --accelerator amd --engine vllm --target hpc\n\n"
    "  exa hardware portable --engine sglang --accelerator amd\n\n"
    "  exa hardware burst train-llm --accelerator nvidia --residency eu-only --allow-burst\n\n"
    "  exa hardware decisions"
)


@app.command("add-pool", epilog=_EXAMPLES)
def add_pool(
    name: str = typer.Argument(..., help="Device-pool name"),
    target: str = typer.Option("hpc", "--target", help="hpc | cloud"),
    accelerator: str = typer.Option(
        "nvidia", "--accelerator", help="nvidia|amd|intel-gaudi|tpu|cpu"
    ),
    capability: list[str] = typer.Option(None, "--capability", help="Capability tag (repeatable)"),
    count: int = typer.Option(1, "--count", help="Devices available"),
    region: str = typer.Option(None, "--region", help="Region (for residency + carbon)"),
    cost_per_hour: float = typer.Option(0.0, "--cost-per-hour", help="Cost per device-hour"),
    carbon_factor: float = typer.Option(0.0, "--carbon-factor", help="gCO2e per device-hour"),
    supports_fractions: bool = typer.Option(
        False, "--supports-fractions", help="Vendor GPU fractioning (MIG)"
    ),
) -> None:
    """Register (or update) a device pool."""
    from examlops.hardware import ACCELERATORS
    from examlops.platform_db import register_device_pool

    if accelerator not in ACCELERATORS:
        _output.error(f"accelerator must be one of {ACCELERATORS}")
        return
    register_device_pool(
        name,
        target=target,
        accelerator=accelerator,
        capabilities=list(capability or []),
        count=count,
        region=region,
        cost_per_hour=cost_per_hour,
        carbon_factor=carbon_factor,
        supports_fractions=supports_fractions,
    )
    _output.ok(
        f"Pool [bold]{name}[/bold]: {count}× {accelerator} on {target}"
        + (f" ({region})" if region else "")
    )


@app.command("pools")
def pools(
    target: str = typer.Option(None, "--target", help="Filter by hpc|cloud"),
    accelerator: str = typer.Option(None, "--accelerator", help="Filter by accelerator"),
) -> None:
    """List registered device pools."""
    from examlops.platform_db import get_device_pools

    rows = get_device_pools(target=target, accelerator=accelerator)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No device pools registered.")
        return
    _output.print_table(
        "Device pools",
        ["name", "target", "accelerator", "count", "region", "$/hr", "gCO2e/hr", "frac"],
        [
            [
                p["name"],
                p["target"],
                p["accelerator"],
                str(p["count"]),
                p.get("region") or "—",
                f"{p['cost_per_hour']:.2f}",
                f"{p['carbon_factor']:.0f}",
                "✓" if p["supports_fractions"] else "—",
            ]
            for p in rows
        ],
    )


@app.command("place")
def place_cmd(
    name: str = typer.Argument(..., help="Workload name"),
    accelerator: str = typer.Option("nvidia", "--accelerator", help="Requested accelerator"),
    engine: str = typer.Option("generic", "--engine", help="Serving/training engine (E2)"),
    target: str = typer.Option(None, "--target", help="hpc | cloud (default: any)"),
    capability: list[str] = typer.Option(
        None, "--capability", help="Required capability (repeatable)"
    ),
    fraction: float = typer.Option(1.0, "--fraction", help="GPU fraction (0<f≤1)"),
) -> None:
    """Place a workload on the best-available compatible device (honest fallback / clear reject)."""
    from examlops.hardware import Placement, Workload, place

    w = Workload(
        name,
        accelerator=accelerator,
        capabilities=list(capability or []),
        target=target,
        engine=engine,
        fraction=fraction,
    )
    result = place(w)
    if _output.json_mode:
        _output.print_json(result.__dict__)
        return
    if isinstance(result, Placement):
        tag = " [yellow](fallback)[/yellow]" if result.fallback else ""
        _output.ok(
            f"{name} → pool [bold]{result.pool}[/bold] · {result.accelerator} on "
            f"{result.target}{tag}"
        )
        if result.region:
            _output.info(
                f"  region={result.region} · ${result.cost_per_hour:.2f}/hr · "
                f"{result.carbon_factor:.0f} gCO2e/hr"
            )
        if not result.fraction_honored:
            _output.warning(f"  {result.note}")
        elif result.note:
            _output.info(f"  {result.note}")
    else:
        _output.error(f"Rejected: {result.reason}")


@app.command("portable")
def portable_cmd(
    engine: str = typer.Option(..., "--engine", help="Engine name"),
    accelerator: str = typer.Option(..., "--accelerator", help="Target accelerator"),
) -> None:
    """Check whether an engine can run on a given accelerator (portability gate)."""
    from examlops.hardware import portable

    ok = portable(engine, accelerator)
    if _output.json_mode:
        _output.print_json({"engine": engine, "accelerator": accelerator, "portable": ok})
        return
    if ok:
        _output.ok(f"{engine} can run on {accelerator}")
    else:
        _output.warning(f"{engine} cannot run on {accelerator}")


@app.command("burst")
def burst_cmd(
    name: str = typer.Argument(..., help="Workload name"),
    accelerator: str = typer.Option("nvidia", "--accelerator", help="Requested accelerator"),
    engine: str = typer.Option("generic", "--engine", help="Engine (E2)"),
    residency: str = typer.Option("open", "--residency", help="open | eu-only | no-egress"),
    allow_burst: bool = typer.Option(False, "--allow-burst", help="Opt in to cloud burst"),
) -> None:
    """Plan a governed HPC→cloud burst (blocked + audited when residency forbids egress)."""
    from examlops.hardware import Placement, Workload, plan_burst

    w = Workload(
        name,
        accelerator=accelerator,
        engine=engine,
        residency=residency,
        allow_burst=allow_burst,
    )
    result = plan_burst(w)
    if _output.json_mode:
        _output.print_json(result.__dict__)
        return
    if isinstance(result, Placement):
        _output.ok(f"{name} burst → cloud pool [bold]{result.pool}[/bold] ({result.region})")
    else:
        _output.warning(f"Burst blocked: {result.reason}")


@app.command("decisions")
def decisions(limit: int = typer.Option(20, "--limit", help="Rows to show")) -> None:
    """Show recent placement decisions."""
    from examlops.platform_db import list_placement_decisions

    rows = list_placement_decisions(limit)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No placement decisions recorded.")
        return
    _output.print_table(
        "Placement decisions",
        ["workload", "requested", "chosen", "pool", "decision", "reason"],
        [
            [
                r["workload"],
                r["accelerator_requested"] or "—",
                r["device_chosen"] or "—",
                r["pool"] or "—",
                r["decision"],
                (r["reason"] or "")[:40],
            ]
            for r in rows
        ],
    )
