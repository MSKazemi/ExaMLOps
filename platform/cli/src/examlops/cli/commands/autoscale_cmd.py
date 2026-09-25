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
    _output.ok(f"Autoscale set for {model}: {min_}–{max_} replicas, target {metric}={target}{stz}")


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
        f"{model}: {decision.current_replicas} {arrow} "
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
    from examlops.autoscale.policy_yaml import effective_config
    from examlops.data.autoscale_desired import get_desired
    from examlops.data.serving import list_scale_events

    cfg = effective_config(model)
    if not cfg:
        _output.info(f"No autoscale policy for {model}.")
        return
    events = list_scale_events(model, last_n=10)
    cs = cold_start_seconds(model)
    desired = get_desired(model)
    if _output.json_mode:
        _output.print_json(
            {"config": cfg, "events": events, "cold_start_s": cs, "desired": desired}
        )
        return
    _output.print_record(
        {
            "model": model,
            "replicas": f"{cfg['min_replicas']}–{cfg['max_replicas']}",
            "target": f"{cfg['target_metric']}={cfg['target_value']}",
            "scale_to_zero_after_s": cfg["scale_to_zero_after_s"] or "disabled",
            "warm_pool": cfg["warm_pool"],
            "gpu_fraction": cfg["gpu_fraction"],
            "policy_source": cfg.get("source", "db"),
            "desired_replicas": desired["replicas"] if desired else "not set",
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
        f"{model}: {s['scale_to_zero_events']} scale-to-zero event(s) → "
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


_RUN_EXAMPLES = (
    "Examples:\n\n"
    "  exa serve autoscale run --once                # preview one cycle (dry run, the default)\n\n"
    "  EXAMLOPS_AUTOSCALE_ENABLED=1 exa serve autoscale run --apply --once\n\n"
    "  EXAMLOPS_AUTOSCALE_ENABLED=1 exa serve autoscale run --apply   # loop until stopped"
)


@app.command("run", epilog=_RUN_EXAMPLES)
def run(
    once: bool = typer.Option(False, "--once", help="Run one cycle and exit (default: loop)"),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Execute decisions (needs EXAMLOPS_AUTOSCALE_ENABLED=1). Default: dry run",
    ),
    applier: str = typer.Option(
        "record",
        "--applier",
        help="record (ledger only) | desired (write desired replicas) | "
        "k8s (patch the predictor Deployment's scale) | ray (not built: refuses)",
    ),
    interval: int = typer.Option(0, "--interval", help="Seconds between cycles (0 = env/30)"),
) -> None:
    """Run the autoscale controller: signals -> decide_scale -> apply (audited, dry run by default)."""
    from examlops.autoscale.controller import (
        AutoscaleController,
        CycleReport,
        PrometheusSignals,
        ScaleApplyError,
        interval_s,
        make_applier,
    )

    try:
        chosen = make_applier(applier)
        check = getattr(chosen, "check", None)
        if apply and callable(check):
            check()  # e.g. the k8s applier resolves (and refuses) its API config up front
    except (ValueError, ScaleApplyError) as exc:
        _output.error(str(exc))
        raise typer.Exit(2) from exc
    ctl = AutoscaleController(PrometheusSignals(), chosen, dry_run=not apply)

    def show(rep: CycleReport) -> None:
        if _output.json_mode:
            _output.print_json(rep.to_dict())
            return
        mode = "dry run" if rep.dry_run else "apply"
        if not rep.ran:
            _output.warning(f"autoscale ({mode}): not run — {rep.note}")
            return
        counts = ", ".join(f"{k}={v}" for k, v in rep.to_dict()["counts"].items() if v)
        _output.info(f"autoscale ({mode}, applier={applier}): {counts or 'no policies'}")
        for r in rep.results:
            if r.outcome != "steady":
                _output.info(f"  {r.model}: {r.outcome} - {r.reason}")

    if once:
        rep = ctl.run_cycle()
        show(rep)
        if not rep.ran or rep.count("failed"):
            raise typer.Exit(1)
        return
    if _output.json_mode:
        _output.error("--json needs --once (a loop prints one document per cycle)")
        raise typer.Exit(2)
    try:
        ctl.run_forever(interval=interval or interval_s(), on_cycle=show)
    except KeyboardInterrupt:
        _output.info("autoscale controller stopped.")


_MANIFEST_EXAMPLES = (
    "Examples:\n\n"
    "  exa serve autoscale manifest JPCP --kind keda\n\n"
    "  exa serve autoscale manifest JPCP --kind knative --out ./k8s/jpcp-autoscale.yaml\n\n"
    "  exa --json serve autoscale manifest JPCP --kind keda"
)


@app.command("manifest", epilog=_MANIFEST_EXAMPLES)
def manifest(
    model: str = typer.Argument(..., help="Model name (needs an autoscale policy)"),
    kind: str = typer.Option(
        "keda", "--kind", help="keda (ScaledObject) | knative (KServe overlay)"
    ),
    target: str = typer.Option(
        None, "--target", help="KEDA scaleTargetRef name (default <model>-predictor)"
    ),
    namespace: str = typer.Option(None, "--namespace", help="Kubernetes namespace"),
    prometheus_url: str = typer.Option(
        "http://prometheus:9090", "--prometheus-url", help="Prometheus address KEDA queries"
    ),
    out: str = typer.Option(None, "--out", help="Write the YAML to this file"),
) -> None:
    """Render a KEDA ScaledObject or Knative/KServe autoscaling overlay from the policy (read-only)."""
    import yaml

    from examlops.autoscale import get_policy
    from examlops.autoscale.manifests import ManifestError, render

    policy = get_policy(model)
    if policy is None:
        _output.error(f"No autoscale policy for {model} - use: exa serve autoscale set {model}")
        raise typer.Exit(1)
    try:
        doc = render(
            kind,
            model,
            policy,
            target_name=target,
            namespace=namespace,
            prometheus_url=prometheus_url,
        )
    except ManifestError as exc:
        _output.error(str(exc), exit_code=2)
    text = yaml.safe_dump(doc, sort_keys=False)
    if out:
        from pathlib import Path

        Path(out).write_text(text)
        _output.ok(f"{doc['kind']} written to {out} (not applied to any cluster)")
        return
    if _output.json_mode:
        _output.print_json(doc)
        return
    typer.echo(text)


@app.command("prefetch")
def prefetch(
    top: int = typer.Option(0, "--top", help="Only the first N entries (0 = all)"),
) -> None:
    """Plan which models to keep warm / pre-pull, from policies + recent traffic (read-only)."""
    from examlops.autoscale import AutoscalePolicy
    from examlops.autoscale.controller import PrometheusSignals, SignalSourceDown
    from examlops.autoscale.policy_yaml import effective_configs
    from examlops.autoscale.prefetch import plan_prefetch

    configs = effective_configs()
    src = PrometheusSignals()
    rps: dict[str, float | None] = {}
    note = ""
    for cfg in configs:
        try:
            rps[str(cfg["model"])] = src.read(
                str(cfg["model"]), AutoscalePolicy.from_config(cfg)
            ).rps
        except SignalSourceDown as exc:
            note = f"traffic unavailable ({exc}); only warm-pool entries are planned"
            break
    plan = plan_prefetch(configs, rps, top=top or None)
    if _output.json_mode:
        _output.print_json({"plan": plan, "note": note})
        return
    if note:
        _output.warning(note)
    if not plan:
        _output.info("Nothing to prefetch (no warm pool and no zero-able model with traffic).")
        return
    _output.print_table(
        "Prefetch plan (read-only; the weight cache is not built)",
        ["Model", "Action", "Replicas", "RPS", "Why"],
        [
            [
                p["model"],
                p["action"],
                str(p["replicas"]),
                "-" if p["rps"] is None else f"{p['rps']:g}",
                p["reason"],
            ]
            for p in plan
        ],
    )


_ACTIVATE_EXAMPLES = (
    "Examples:\n\n"
    "  exa serve autoscale activate JPCP                     # wake a scaled-to-zero model\n\n"
    "  exa serve autoscale activate JPCP --applier k8s --timeout 300\n\n"
    "  exa --json serve autoscale activate JPCP"
)


@app.command("activate", epilog=_ACTIVATE_EXAMPLES)
def activate(
    model: str = typer.Argument(..., help="Model name (needs an autoscale policy)"),
    applier: str = typer.Option(
        "desired", "--applier", help="desired | k8s | record — who owns the replicas"
    ),
    timeout: float = typer.Option(120.0, "--timeout", help="Seconds to wait for readiness"),
) -> None:
    """Wake a model from zero replicas and wait until it is ready; cold start is measured + audited."""
    from examlops.autoscale.activator import Activator, ActivatorError
    from examlops.autoscale.controller import ScaleApplyError, make_applier

    if timeout <= 0 or timeout > 3600:
        _output.error("--timeout must be in (0, 3600] seconds", exit_code=2)
    try:
        chosen = make_applier(applier)
        check = getattr(chosen, "check", None)
        if callable(check):
            # A misconfigured k8s API must be a refusal here, not "replicas unknown — nothing to
            # do" (exit 0) once ensure_warm reads it as an unknown replica count.
            check()
        act = Activator(chosen)
    except (ValueError, ScaleApplyError) as exc:
        _output.error(str(exc), exit_code=2)
        return
    try:
        res = act.ensure_warm(model, timeout)
    except ActivatorError as exc:
        _output.error(str(exc), exit_code=1)
        return
    if _output.json_mode:
        _output.print_json(res.to_dict())
        return
    if res.woke:
        _output.ok(f"{res.model}: woke 0→{res.replicas} in {res.cold_start_s:.2f}s (cold start)")
    else:
        _output.info(f"{res.model}: {res.note} — nothing to do")
