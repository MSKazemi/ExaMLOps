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
    from examlops.platform_db import set_fairness_config, write_audit_event

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
        f"Fairness config for [bold]{model}[/bold]: slices={', '.join(attr)} "
        f"threshold={threshold}{' (gates promotion)' if gate else ''}"
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
        _output.info(f"[bold]{res.slice_attr}[/bold] — {len(res.slices)} slice(s)")
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
