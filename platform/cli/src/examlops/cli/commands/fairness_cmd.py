"""C8 — `exa fairness`: subgroup performance & fairness disparity (ADR 0025).

Slice performance by declared attributes, compute fairness disparities (demographic
parity, equalized odds, selection-rate range), and gate/alert on them. Tenant-scoped
(D6) and audited (D4). Feeds A6 model cards and D1 EU-AI-Act reports.
"""

from __future__ import annotations

import os

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Fairness — per-slice performance + disparity monitoring & gating",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa fairness config JPCP --attr region --attr tier --threshold 0.1 --gate\n\n"
    "  exa fairness slice JPCP region\n\n"
    "  exa fairness report JPCP\n\n"
    "  exa --json fairness report JPCP"
)


@app.command("config", epilog=_EXAMPLES)
def config(
    model: str = typer.Argument(..., help="Model name"),
    attr: list[str] = typer.Option(..., "--attr", help="Slicing attribute (repeatable)"),
    threshold: float = typer.Option(0.1, "--threshold", help="Max allowed disparity"),
    min_samples: int = typer.Option(30, "--min-samples", help="Noise guard per slice"),
    gate: bool = typer.Option(False, "--gate", help="Gate promotion on disparity (C3)"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope (D6)"),
) -> None:
    """Declare slicing attributes + disparity threshold for a model (R1)."""
    from examlops.data.audit import write_audit_event
    from examlops.data.governance import set_fairness_config

    set_fairness_config(
        model,
        list(attr),
        tenant=tenant,
        threshold=threshold,
        min_samples=min_samples,
        gate_promotion=gate,
    )
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    write_audit_event(
        "cli",
        actor,
        "fairness_config",
        model,
        {"slice_attrs": list(attr), "threshold": threshold, "gate": gate},
    )
    _output.ok(
        f"Fairness config for {model}: slices={', '.join(attr)} "
        f"threshold={threshold}{' (gates promotion)' if gate else ''}"
    )


@app.command("show")
def show(
    model: str = typer.Argument(..., help="Model name"),
) -> None:
    """Show the slice registry actually in force, and where it came from (ADR 0025 clause 1).

    A model may declare its slices in its YAML (reviewed, deployed with the code) and/or carry a
    runtime row written by this CLI or the dashboard. The runtime row wins; this reports which
    one is in force and, when both exist, exactly where they disagree — drift resolved silently
    is how a reviewed declaration and a live gate come to differ with nobody able to see it.
    """
    from examlops.fairness import effective_fairness_config, fairness_config_drift

    cfg, source = effective_fairness_config(model)
    drift = fairness_config_drift(model)
    if _output.json_mode:
        _output.print_json({"model": model, "source": source, "config": cfg, "drift": drift})
        return
    if cfg is None:
        _output.ok(
            f"No slice registry for {model} — neither a `fairness:` block in its model YAML "
            "nor a runtime config. Fairness gating does not apply."
        )
        return
    _output.print_table(
        f"Fairness slice registry — {model}",
        ["Source", "Slices", "Threshold", "Min samples", "Gates promotion"],
        [
            [
                source,
                ", ".join(cfg["slice_attrs"]),
                str(cfg["threshold"]),
                str(cfg["min_samples"]),
                "yes" if cfg["gate_promotion"] else "no",
            ]
        ],
    )
    for line in drift:
        _output.warning(f"declaration drift — {line}")


@app.command("apply")
def apply(
    model: str = typer.Argument(..., help="Model name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope (D6)"),
) -> None:
    """Materialise the model YAML's `fairness:` block as the runtime config (audited).

    Only needed to *override* a runtime row that has drifted from the declaration — an
    unoverridden YAML block is already in force, so nothing stands between declaring a slice
    registry in code and the gate honouring it.
    """
    from examlops.data.audit import write_audit_event
    from examlops.data.governance import set_fairness_config
    from examlops.fairness import _yaml_fairness_config

    declared = _yaml_fairness_config(model)
    if not declared:
        _output.error(
            f"{model} has no valid `fairness:` block in its model YAML.",
            hint="Add one, or declare the registry directly with `exa fairness config`.",
        )
    set_fairness_config(
        model,
        declared["slice_attrs"],
        tenant=tenant,
        threshold=declared["threshold"],
        min_samples=declared["min_samples"],
        gate_promotion=declared["gate_promotion"],
        enabled=declared["enabled"],
    )
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    write_audit_event(
        "cli", actor, "fairness_config_applied", model, {"source": "yaml", **declared}
    )
    _output.ok(
        f"Applied {model}'s declared slice registry: "
        f"slices={', '.join(declared['slice_attrs'])} threshold={declared['threshold']}"
    )


@app.command("slice")
def slice_cmd(
    model: str = typer.Argument(..., help="Model name"),
    attr: str = typer.Argument(..., help="Slicing attribute"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Show per-slice performance for one slicing attribute (R2, GWT-1)."""
    from examlops.fairness import slice_metrics

    res = slice_metrics(model, attr, tenant=tenant)
    if _output.json_mode:
        _output.print_json(res.as_dict())
        return
    if not res.slices:
        _output.info(f"No fairness samples recorded for {model}/{attr}.")
        return
    _output.print_table(
        f"Subgroup Performance — {model} by {attr}",
        ["Slice", "N", "Accuracy", "Error", "Sel.Rate", "TPR", "FPR", "Guard"],
        [
            [
                s.slice_value,
                str(s.n),
                f"{s.accuracy:.3f}" if s.accuracy is not None else "—",
                f"{s.error:.3f}" if s.error is not None else "—",
                f"{s.selection_rate:.3f}" if s.selection_rate is not None else "—",
                f"{s.tpr:.3f}" if s.tpr is not None else "—",
                f"{s.fpr:.3f}" if s.fpr is not None else "—",
                "low-n" if s.below_min else "ok",
            ]
            for s in res.slices
        ],
    )
    _print_disparities(res)


@app.command("report")
def report(
    model: str = typer.Argument(..., help="Model name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Full fairness report across all declared slice attributes (R5)."""
    from examlops.fairness import fairness_report

    results = fairness_report(model, tenant=tenant)
    if _output.json_mode:
        _output.print_json([r.as_dict() for r in results])
        return
    if not results:
        _output.info(
            f"No slice attributes declared for {model} — use: exa fairness config {model} --attr <attr>"
        )
        return
    for res in results:
        _output.info(f"{res.slice_attr} — {len(res.slices)} slice(s)")
        _print_disparities(res)


def _print_disparities(res) -> None:
    def fmt(v):
        return f"{v:.3f}" if v is not None else "—"

    line = (
        f"  DP diff={fmt(res.demographic_parity_diff)}  "
        f"EO diff={fmt(res.equalized_odds_diff)}  "
        f"sel-rate range={fmt(res.selection_rate_range)}  "
        f"acc range={fmt(res.accuracy_range)}  (threshold {res.threshold})"
    )
    if res.disparity_exceeded:
        _output.warning(line + "  ⚠ DISPARITY EXCEEDED")
    else:
        _output.info(line)
