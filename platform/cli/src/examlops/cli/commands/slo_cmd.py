"""C6 — `exa slo`: model-quality SLOs/SLIs & burn-rate alerting (ADR 0023).

Declare OpenSLO-style SLIs/SLOs per model, generate promtool-valid Prometheus
recording + burn-rate rules, and inspect live error-budget status. Budget exhaustion
can gate promotion (C3) when `EXAMLOPS_SLO_GATE_ENABLED` is set.
"""

from __future__ import annotations

import os

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Model-quality SLOs — declare, generate rules, and track error budgets",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa slo set JPCP latency-p99 --target 0.99 --window 30d\n\n"
    "  exa slo apply slos.yaml\n\n"
    "  exa slo status JPCP\n\n"
    "  exa slo generate JPCP latency-p99 --out slo_rules.yml\n\n"
    "  exa slo burn JPCP"
)


@app.command("set", epilog=_EXAMPLES)
def set_slo(
    model: str = typer.Argument(..., help="Model name"),
    name: str = typer.Argument(..., help="SLO name (e.g. latency-p99, groundedness)"),
    target: float = typer.Option(0.99, "--target", help="Objective ratio 0..1"),
    window: str = typer.Option("30d", "--window", help="Rolling window (e.g. 30d)"),
    sli_source: str = typer.Option(
        "prometheus", "--source", help="c1|c2|c5|availability|prometheus"
    ),
    sli_query: str = typer.Option(None, "--query", help="PromQL SLI expression (good ratio)"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope (D6)"),
    gate: bool = typer.Option(False, "--gate", help="Gate promotion when budget exhausted (C3)"),
) -> None:
    """Declare or version-bump one SLO spec (R1)."""
    from examlops.slo import apply_spec

    apply_spec(
        {
            "model": model,
            "name": name,
            "target": target,
            "window": window,
            "sli_source": sli_source,
            "sli_query": sli_query,
            "tenant": tenant,
            "gate_promotion": gate,
        }
    )
    _output.ok(f"SLO '{name}' set for {model} (target={target}, window={window})")


@app.command("apply")
def apply(
    path: str = typer.Argument(..., help="OpenSLO-style YAML file (slos: [...])"),
) -> None:
    """Apply all SLO specs from a YAML file (R1)."""
    from examlops.slo import apply_spec, load_specs

    specs = load_specs(path)
    for spec in specs:
        apply_spec(spec)
    _output.ok(f"Applied {len(specs)} SLO spec(s) from {path}")


@app.command("list")
def list_slos(
    model: str = typer.Option(None, "--model", help="Filter to one model"),
    tenant: str = typer.Option(None, "--tenant", help="Filter to one tenant"),
) -> None:
    """List declared SLO specs."""
    from examlops.data.governance import list_slo_specs

    specs = list_slo_specs(model=model, tenant=tenant)
    if _output.json_mode:
        _output.print_json(specs)
        return
    if not specs:
        _output.info("No SLOs declared — use: exa slo set <MODEL> <NAME> --target 0.99")
        return
    _output.print_table(
        "SLO Specs",
        ["Model", "Name", "Tenant", "Target", "Window", "Source", "Ver", "Gate"],
        [
            [
                s["model"],
                s["name"],
                s["tenant"],
                f"{s['target']:.3f}",
                s["window"],
                s["sli_source"],
                str(s["version"]),
                "yes" if s["gate_promotion"] else "no",
            ]
            for s in specs
        ],
    )


@app.command("status")
def status(
    model: str = typer.Argument(..., help="Model name"),
    name: str = typer.Option(None, "--name", help="One SLO (default: all for the model)"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Show SLI, remaining error budget, and burn rate per SLO (R5)."""
    from examlops.slo import slo_status

    statuses = slo_status(model, name, tenant=tenant)
    if _output.json_mode:
        _output.print_json([s.as_dict() for s in statuses])
        return
    if not statuses:
        _output.info(f"No SLOs declared for {model}.")
        return
    _output.print_table(
        f"SLO Status — {model}",
        ["SLO", "Target", "SLI", "Budget Left", "Burn", "Status"],
        [
            [
                s.name,
                f"{s.target:.3f}",
                f"{s.sli:.4f}",
                f"{s.budget_remaining:.0%}",
                f"{s.burn_rate:.2f}x" if s.burn_rate != float("inf") else "∞",
                "OK" if s.ok else "BREACH",
            ]
            for s in statuses
        ],
    )


@app.command("generate")
def generate(
    model: str = typer.Argument(..., help="Model name"),
    name: str = typer.Argument(..., help="SLO name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
    out: str = typer.Option(None, "--out", help="Write rules YAML to this file"),
) -> None:
    """Generate promtool-valid Prometheus recording + burn-rate rules (R2/R3)."""
    from examlops.data.governance import get_slo_spec
    from examlops.slo import generate_rules

    spec = get_slo_spec(model, name, tenant)
    if spec is None:
        _output.error(f"No SLO '{name}' declared for {model} — use: exa slo set")
        return
    rules = generate_rules(spec)
    yaml_text = rules.to_yaml()
    if out:
        with open(out, "w") as fh:
            fh.write(yaml_text)
        _output.ok(f"Wrote {len(rules.groups)} rule group(s) to {out}")
        return
    if _output.json_mode:
        _output.print_json({"groups": rules.groups})
        return
    _output.info(yaml_text)


@app.command("burn")
def burn(
    model: str = typer.Argument(..., help="Model name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Show which SLOs are burning budget (and would page) (R3)."""
    from examlops.slo import slo_status

    statuses = slo_status(model, tenant=tenant)
    burning = [s for s in statuses if s.burn_rate > 1.0]
    if _output.json_mode:
        _output.print_json([s.as_dict() for s in burning])
        return
    if not burning:
        _output.ok(f"No SLOs burning budget for {model}.")
        return
    for s in burning:
        sev = "PAGE" if s.burn_rate >= 6.0 else "WARN"
        _output.warning(
            f"[{sev}] {s.name}: burning {s.burn_rate:.2f}x — budget left {s.budget_remaining:.0%}"
        )


@app.command("record")
def record(
    model: str = typer.Argument(..., help="Model name"),
    name: str = typer.Argument(..., help="SLO name"),
    good: float = typer.Argument(..., help="Good events this interval"),
    total: float = typer.Argument(..., help="Total events this interval"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Record one SLI measurement interval (R4) — feeds budget + burn rate."""
    from examlops.data.governance import record_slo_sample

    record_slo_sample(model, name, good, total, tenant=tenant)
    _output.ok(f"Recorded SLI sample for {model}/{name}: {good}/{total}")


# Env flag consulted by the C3 promotion gate (imported for discoverability).
SLO_GATE_ENV = "EXAMLOPS_SLO_GATE_ENABLED"


def gate_enabled() -> bool:
    return os.getenv(SLO_GATE_ENV, "").lower() in ("1", "true", "yes", "on")
