"""E7 — `exa federated`: federated & privacy-preserving training (ADR 0040).

Initialize a federated run across sites that cannot share raw data, aggregate a round of
site updates (FedAvg / FedProx / Byzantine-robust), inspect the tracked (ε, δ) differential-
privacy budget, and view run status. Raw data never leaves a site — only signed model updates
are aggregated; unauthorized/unsigned sites are rejected + audited.
"""

from __future__ import annotations

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Federated & privacy-preserving training (E7)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa federated init --site siteA --site siteB --strategy fedavg\n\n"
    "  exa federated init --site siteA --site siteB --dp --epsilon-per-round 0.5 --secure-agg\n\n"
    "  exa federated round fed-fedavg-2sites --update siteA:0.1,0.2:100 --update siteB:0.3,0.4:150\n\n"
    "  exa federated budget fed-fedavg-2sites\n\n"
    "  exa federated status fed-fedavg-2sites"
)


@app.command("init", epilog=_EXAMPLES)
def init(
    site: list[str] = typer.Option(..., "--site", help="Participating site (repeatable)"),
    strategy: str = typer.Option("fedavg", "--strategy", help="fedavg | fedprox | robust"),
    run_id: str = typer.Option(None, "--run-id", help="Explicit run id (else derived)"),
    dp: bool = typer.Option(False, "--dp", help="Enable differential privacy accounting"),
    epsilon_per_round: float = typer.Option(
        0.5, "--epsilon-per-round", help="DP ε spent per round"
    ),
    delta: float = typer.Option(1e-5, "--delta", help="DP δ"),
    secure_agg: bool = typer.Option(False, "--secure-agg", help="Hide per-site updates"),
    unauthorized: list[str] = typer.Option(
        None, "--unauthorized", help="Site to register but NOT authorize (repeatable)"
    ),
) -> None:
    """Initialize a federated run: register sites + privacy config."""
    from examlops.federated import federated_init

    authorized = [s for s in site if s not in (unauthorized or [])]
    dp_cfg = {"epsilon_per_round": epsilon_per_round, "delta": delta} if dp else None
    run = federated_init(
        list(site),
        strategy=strategy,
        run_id=run_id,
        dp=dp_cfg,
        secure_agg=secure_agg,
        authorized_sites=authorized,
    )
    if _output.json_mode:
        _output.print_json(
            {
                "run_id": run.run_id,
                "strategy": run.strategy,
                "dp_enabled": run.dp_enabled,
                "secure_agg": run.secure_agg,
                "sites": run.sites,
            }
        )
        return
    _output.ok(f"Federated run {run.run_id} · {run.strategy} · {len(run.sites)} sites")
    if run.dp_enabled:
        _output.info(f"  DP on: ε/round={epsilon_per_round}, δ={delta}")
    if run.secure_agg:
        _output.info("  secure aggregation on: per-site updates hidden")


@app.command("round")
def round_cmd(
    run_id: str = typer.Argument(..., help="Federated run id"),
    update: list[str] = typer.Option(
        ..., "--update", help="site:w1,w2,…:num_samples[:loss] (repeatable)"
    ),
    unsigned: list[str] = typer.Option(
        None, "--unsigned", help="Treat this site's update as unsigned (repeatable)"
    ),
) -> None:
    """Aggregate one round of site updates (rejects unauthorized/unsigned sites)."""
    from examlops.federated import SiteUpdate, run_round

    unsigned_set = set(unsigned or [])
    updates = []
    for u in update:
        parts = u.split(":")
        site = parts[0]
        weights = [float(x) for x in parts[1].split(",")]
        num_samples = int(parts[2]) if len(parts) > 2 else 1
        loss = float(parts[3]) if len(parts) > 3 else 0.0
        updates.append(
            SiteUpdate(site, weights, num_samples, loss=loss, signed=site not in unsigned_set)
        )
    result = run_round(run_id, updates)
    if _output.json_mode:
        _output.print_json(
            {
                "run_id": result.run_id,
                "round": result.round_num,
                "global_weights": result.global_weights,
                "global_loss": result.global_loss,
                "sites_participated": result.sites_participated,
                "epsilon": result.epsilon,
                "delta": result.delta,
                "rejected": result.rejected,
            }
        )
        return
    _output.ok(
        f"Round {result.round_num}: {result.sites_participated} sites · "
        f"loss={result.global_loss:.4f}"
    )
    if result.epsilon:
        _output.info(f"  DP budget spent: ε={result.epsilon:.3f}, δ={result.delta}")
    if result.rejected:
        _output.warning(f"  rejected sites (unauthorized/unsigned): {', '.join(result.rejected)}")


@app.command("budget")
def budget(run_id: str = typer.Argument(..., help="Federated run id")) -> None:
    """Show the tracked differential-privacy (ε, δ) budget."""
    from examlops.federated import privacy_budget

    b = privacy_budget(run_id)
    if _output.json_mode:
        _output.print_json(b)
        return
    if not b.get("dp_enabled"):
        _output.info(b.get("note", "no differential privacy configured"))
        return
    _output.print_record(
        {
            "run_id": b["run_id"],
            "epsilon": round(b["epsilon"], 4),
            "delta": b["delta"],
            "rounds": b["rounds"],
        }
    )


@app.command("status")
def status(run_id: str = typer.Argument(..., help="Federated run id")) -> None:
    """Show run config, sites, and completed rounds."""
    from examlops.federated import federated_status

    st = federated_status(run_id)
    if _output.json_mode:
        _output.print_json(st)
        return
    if st["run"] is None:
        _output.error(f"Unknown federated run {run_id!r}")
        return
    run = st["run"]
    _output.print_record(
        {
            "run_id": run["run_id"],
            "strategy": run["strategy"],
            "dp_enabled": bool(run["dp_enabled"]),
            "secure_agg": bool(run["secure_agg"]),
            "rounds_completed": run["rounds_completed"],
            "status": run["status"],
        }
    )
    if st["sites"]:
        _output.print_table(
            "Sites",
            ["site", "authorized"],
            [[s["site"], "✓" if s["authorized"] else "✗"] for s in st["sites"]],
        )
    if st["rounds"]:
        _output.print_table(
            "Rounds",
            ["round", "loss", "sites", "epsilon"],
            [
                [
                    str(r["round_num"]),
                    f"{r['global_metric']:.4f}" if r["global_metric"] is not None else "—",
                    str(r["sites_participated"]),
                    f"{r['epsilon']:.3f}" if r["epsilon"] is not None else "—",
                ]
                for r in st["rounds"]
            ],
        )
