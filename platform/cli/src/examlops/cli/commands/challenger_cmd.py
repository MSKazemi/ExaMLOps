"""C7 — `exa serve challenger`: champion-challenger scoreboard & promotion (ADR 0024).

Enables an isolated challenger, scores it against the champion via the phase-24 A/B-stats
engine as labels arrive, and proposes promotion (C3) when it wins with significance and
no C6 SLO regression. Runs are audited (D4) and tenant-scoped (D6).
"""

from __future__ import annotations

import os

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Champion-challenger — score a shadow challenger and promote on a significant win",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa serve challenger enable JPCP --version 18 --mirror 50\n\n"
    "  exa serve challenger status JPCP\n\n"
    "  exa serve challenger promote JPCP\n\n"
    "  exa serve challenger disable JPCP"
)


@app.command("enable", epilog=_EXAMPLES)
def enable(
    model: str = typer.Argument(..., help="Model name"),
    version: str = typer.Option(..., "--version", help="Challenger MLflow version"),
    mirror: int = typer.Option(100, "--mirror", help="Percent of traffic to mirror (0-100)"),
    min_delta: float = typer.Option(0.0, "--min-delta", help="Min error reduction to win"),
    alpha: float = typer.Option(0.05, "--alpha", help="Significance level"),
    min_samples: int = typer.Option(100, "--min-samples", help="Min labelled samples to decide"),
    auto_promote: bool = typer.Option(False, "--auto-promote", help="Promote automatically on win"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope (D6)"),
) -> None:
    """Enable a challenger and declare its promotion policy (R1/R5)."""
    from examlops.champion_challenger import enable_shadow

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    enable_shadow(
        model,
        version,
        mirror,
        tenant=tenant,
        min_delta=min_delta,
        alpha=alpha,
        min_samples=min_samples,
        auto_promote=auto_promote,
        actor=actor,
    )
    _output.ok(
        f"Challenger v{version} enabled for [bold]{model}[/bold] "
        f"(mirror {mirror}%, α={alpha}, N≥{min_samples}"
        f"{', auto-promote' if auto_promote else ''})"
    )


@app.command("status")
def status(
    model: str = typer.Argument(..., help="Model name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Show the champion-challenger scoreboard: delta, p-value, N, SLO (R4)."""
    from examlops.champion_challenger import challenger_status

    st = challenger_status(model, tenant=tenant)
    if st is None:
        _output.info(f"No challenger configured for {model}.")
        return
    if _output.json_mode:
        _output.print_json(st.as_dict())
        return
    _output.print_record(
        {
            "model": st.model,
            "challenger": st.challenger_version,
            "n_labelled": st.n,
            "champion_error": f"{st.champion_error:.4f}" if st.champion_error is not None else "—",
            "challenger_error": (
                f"{st.challenger_error:.4f}" if st.challenger_error is not None else "—"
            ),
            "delta (champ-chall)": f"{st.delta:.4f}" if st.delta is not None else "—",
            "p_value": f"{st.p_value:.4f}" if st.p_value is not None else "—",
            "significant": "yes" if st.significant else "no",
            "slo_ok": "yes" if st.slo_ok else "no",
            "policy_met": "yes" if st.policy_met else "no",
        }
    )
    if st.policy_met:
        _output.ok("Policy met — challenger is ready to promote (exa serve challenger promote).")


@app.command("promote")
def promote(
    model: str = typer.Argument(..., help="Model name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Propose promotion via C3 if the policy is met and no SLO regression (R5/R6)."""
    from examlops.champion_challenger import maybe_promote

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    proposal = maybe_promote(model, tenant=tenant, actor=actor)
    if proposal is None:
        _output.warning(
            f"Promotion policy NOT met for {model} — see: exa serve challenger status {model}"
        )
        raise typer.Exit(1)
    if _output.json_mode:
        _output.print_json(
            {
                "model": proposal.model,
                "challenger_version": proposal.challenger_version,
                "delta": proposal.delta,
                "p_value": proposal.p_value,
                "n": proposal.n,
                "auto": proposal.auto,
                "reason": proposal.reason,
            }
        )
        return
    _output.ok(f"Promotion proposed for {model} v{proposal.challenger_version}: {proposal.reason}")
    if proposal.auto:
        _output.info(
            f"auto-promote is on — run: exa pipeline promote {model.lower()} "
            f"--if-rmse-lt <threshold>  (C3 gate applies)"
        )


@app.command("disable")
def disable(
    model: str = typer.Argument(..., help="Model name"),
) -> None:
    """Disable the challenger for a model."""
    from examlops.champion_challenger import platform_db

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    platform_db.disable_challenger(model, updated_by=actor)
    platform_db.write_audit_event("cli", actor, "challenger_disable", model, None)
    _output.ok(f"Challenger disabled for {model}")


@app.command("list")
def list_challengers(
    tenant: str = typer.Option(None, "--tenant", help="Filter to one tenant"),
) -> None:
    """List configured challengers."""
    from examlops.platform_db import list_challenger_configs

    rows = list_challenger_configs(tenant=tenant)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No challengers configured.")
        return
    _output.print_table(
        "Challengers",
        ["Model", "Version", "Mirror%", "α", "N≥", "Auto", "Enabled"],
        [
            [
                r["model"],
                r["challenger_version"],
                str(r["mirror_pct"]),
                f"{r['alpha']:.2f}",
                str(r["min_samples"]),
                "yes" if r["auto_promote"] else "no",
                "yes" if r["enabled"] else "no",
            ]
            for r in rows
        ],
    )
