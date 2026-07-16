"""E5 — `exa serve autoscale`: per-model autoscaling & scale-to-zero (ADR 0031).

Declare a metric-driven autoscale policy (min/max/target, scale-to-zero, warm pool,
anti-thrash windows), simulate a scaling decision, and inspect scale events + savings.
Scale events are audited (D4); scale-to-zero savings flow into FinOps.
"""

from __future__ import annotations

import os

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Autoscaling — per-model replicas, scale-to-zero, warm pool",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa serve autoscale set JPCP --min 0 --max 8 --metric queue_depth --target 10 "
    "--scale-to-zero-after 300\n\n"
    "  exa serve autoscale simulate JPCP --replicas 2 --observed 45\n\n"
    "  exa serve autoscale status JPCP\n\n"
    "  exa serve autoscale savings JPCP"
)


@app.command("set", epilog=_EXAMPLES)
def set_cmd(
    model: str = typer.Argument(..., help="Model name"),
    min_: int = typer.Option(1, "--min", help="Minimum replicas (0 enables scale-to-zero floor)"),
    max_: int = typer.Option(4, "--max", help="Maximum replicas"),
    metric: str = typer.Option("queue_depth", "--metric", help="rps|queue_depth|gpu_util|p95"),
    target: float = typer.Option(10.0, "--target", help="Target value for the metric"),
    scale_to_zero_after: int = typer.Option(
        0, "--scale-to-zero-after", help="Idle seconds before scaling to zero (0 disables)"
    ),
    warm_pool: int = typer.Option(
        0, "--warm-pool", help="Warm replicas to keep (avoid cold start)"
    ),
    gpu_fraction: float = typer.Option(1.0, "--gpu-fraction", help="E3 GPU fraction per replica"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Declare a per-model autoscale policy (R1)."""
    from examlops.autoscale import set_policy

    set_policy(
        model,
        tenant=tenant,
        min_replicas=min_,
        max_replicas=max_,
        target_metric=metric,
        target_value=target,
        scale_to_zero_after_s=scale_to_zero_after,
        warm_pool=warm_pool,
        gpu_fraction=gpu_fraction,
    )
    stz = f", scale-to-zero after {scale_to_zero_after}s" if scale_to_zero_after else ""
    _output.ok(
        f"Autoscale set for [bold]{model}[/bold]: {min_}–{max_} replicas, "
        f"target {metric}={target}{stz}"
    )


@app.command("simulate")
def simulate(
    model: str = typer.Argument(..., help="Model name"),
    replicas: int = typer.Option(..., "--replicas", help="Current replica count"),
    observed: float = typer.Option(..., "--observed", help="Observed metric value"),
    idle: float = typer.Option(0.0, "--idle", help="Idle seconds (for scale-to-zero)"),
    since_last: float = typer.Option(1e9, "--since-last", help="Seconds since last scale"),
) -> None:
    """Compute the scaling decision for a given state (pure, anti-thrash aware) (R2/R3)."""
    from examlops.autoscale import decide_scale, get_policy

    policy = get_policy(model)
    if policy is None:
        _output.error(f"No autoscale policy for {model} — use: exa serve autoscale set {model}")
        return
    decision = decide_scale(
        replicas, observed, policy, idle_seconds=idle, seconds_since_last_scale=since_last
    )
    if _output.json_mode:
        _output.print_json(
            {
                "desired_replicas": decision.desired_replicas,
                "current_replicas": decision.current_replicas,
                "changed": decision.changed,
                "reason": decision.reason,
                "blocked_by": decision.blocked_by,
            }
        )
        return
    arrow = "→" if decision.changed else "="
    _output.info(
        f"[bold]{model}[/bold]: {decision.current_replicas} {arrow} "
        f"{decision.desired_replicas} replicas — {decision.reason}"
    )
    if decision.blocked_by:
        _output.warning(f"  held by anti-thrash: {decision.blocked_by}")


@app.command("status")
def status(
    model: str = typer.Argument(..., help="Model name"),
) -> None:
    """Show the autoscale policy + recent scale events + cold-start time."""
    from examlops.autoscale import cold_start_seconds
    from examlops.platform_db import get_autoscale_config, list_scale_events

    cfg = get_autoscale_config(model)
    if not cfg:
        _output.info(f"No autoscale policy for {model}.")
        return
    events = list_scale_events(model, last_n=10)
    cs = cold_start_seconds(model)
    if _output.json_mode:
        _output.print_json({"config": cfg, "events": events, "cold_start_s": cs})
        return
    _output.print_record(
        {
            "model": model,
            "replicas": f"{cfg['min_replicas']}–{cfg['max_replicas']}",
            "target": f"{cfg['target_metric']}={cfg['target_value']}",
            "scale_to_zero_after_s": cfg["scale_to_zero_after_s"] or "disabled",
            "warm_pool": cfg["warm_pool"],
            "gpu_fraction": cfg["gpu_fraction"],
            "mean_cold_start_s": f"{cs:.2f}" if cs is not None else "not measured",
        }
    )
    if events:
        _output.print_table(
            "Recent Scale Events",
            ["Time", "From", "To", "Reason"],
            [
                [e["ts"], str(e["from_replicas"]), str(e["to_replicas"]), (e["reason"] or "")[:40]]
                for e in events
            ],
        )


@app.command("savings")
def savings(
    model: str = typer.Argument(..., help="Model name"),
    gpu_cost: float = typer.Option(2.0, "--gpu-cost", help="GPU cost per hour"),
) -> None:
    """Estimate FinOps savings from scale-to-zero (R7)."""
    from examlops.autoscale import scale_to_zero_savings

    s = scale_to_zero_savings(model, gpu_cost_per_hour=gpu_cost)
    if _output.json_mode:
        _output.print_json(s)
        return
    _output.info(
        f"[bold]{model}[/bold]: {s['scale_to_zero_events']} scale-to-zero event(s) → "
        f"{s['saved_gpu_hours']} GPU-hours saved (${s['saved_cost']})."
    )


# Convenience: record an executed scale (used by controllers/tests).
@app.command("record")
def record(
    model: str = typer.Argument(..., help="Model name"),
    from_replicas: int = typer.Argument(..., help="Replicas before"),
    to_replicas: int = typer.Argument(..., help="Replicas after"),
    reason: str = typer.Option("manual", "--reason", help="Why the scale happened"),
    cold_start: float = typer.Option(None, "--cold-start", help="Measured cold-start seconds"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Record an executed scale event (audited D4)."""
    from examlops.autoscale import ScaleDecision, apply_scale

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    decision = ScaleDecision(to_replicas, from_replicas, reason, from_replicas != to_replicas)
    apply_scale(model, from_replicas, decision, tenant=tenant, cold_start_s=cold_start, actor=actor)
    _output.ok(f"Recorded scale {from_replicas}→{to_replicas} for {model}.")
