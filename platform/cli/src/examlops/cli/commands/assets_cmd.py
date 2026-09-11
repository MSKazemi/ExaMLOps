"""A4 — `exa assets`: declarative asset-centric pipelines (ADR 0036).

Declare datasets/features/models as assets with a freshness-tracked DAG; materialize a
target asset and only its stale ancestors (selective/incremental). Each materialization
emits OpenLineage (A2), is policy-governed (D5), and audited (D4). The existing
`exa pipeline run` path is unchanged.
"""

from __future__ import annotations

import os

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Asset-centric pipelines — freshness DAG + selective materialization (A4)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa assets declare PM100 --kind dataset\n\n"
    "  exa assets declare jpcp_features --kind feature --deps PM100\n\n"
    "  exa assets declare jpcp_model --kind model --deps jpcp_features\n\n"
    "  exa assets status\n\n"
    "  exa assets materialize jpcp_model\n\n"
    "  exa assets graph"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


@app.command("declare", epilog=_EXAMPLES)
def declare(
    name: str = typer.Argument(..., help="Asset name"),
    kind: str = typer.Option("model", "--kind", help="dataset | feature | model"),
    deps: str = typer.Option("", "--deps", help="Comma-separated upstream asset names"),
    description: str = typer.Option(None, "--description", help="Human description"),
) -> None:
    """Declare an asset and its upstream dependencies (R1)."""
    from examlops.assets import declare_asset

    dep_list = [d.strip() for d in deps.split(",") if d.strip()]
    declare_asset(name, kind=kind, deps=dep_list, description=description)
    _output.ok(f"Declared {kind} asset {name}" + (f" ← {', '.join(dep_list)}" if dep_list else ""))


@app.command("list")
def list_cmd() -> None:
    """List declared assets with their current version."""
    from examlops.data.data_assets import list_assets

    assets = list_assets()
    if _output.json_mode:
        _output.print_json(assets)
        return
    if not assets:
        _output.info("No assets declared. Use: exa assets declare <name> --kind …")
        return
    _output.print_table(
        "Assets",
        ["Name", "Kind", "Version", "Deps"],
        [
            [a["name"], a["kind"], str(a["current_version"]), ", ".join(a["deps"]) or "—"]
            for a in assets
        ],
    )


@app.command("status")
def status(
    name: str = typer.Argument(None, help="Asset name (all assets if omitted)"),
) -> None:
    """Show the freshness graph — fresh/stale + why (R5/GWT-2)."""
    from examlops.assets import asset_status
    from examlops.data.data_assets import list_assets

    names = [name] if name else [a["name"] for a in list_assets()]
    if not names:
        _output.info("No assets declared.")
        return
    results = [asset_status(n) for n in names]
    if _output.json_mode:
        _output.print_json(
            [
                {"name": r.name, "fresh": r.fresh, "version": r.version, "reasons": r.reasons}
                for r in results
            ]
        )
        return
    _output.print_table(
        "Asset Freshness",
        ["Asset", "Version", "State", "Why"],
        [
            [r.name, str(r.version), "fresh" if r.fresh else "STALE", "; ".join(r.reasons) or "—"]
            for r in results
        ],
    )


@app.command("materialize")
def materialize(
    name: str = typer.Argument(..., help="Target asset"),
    force: bool = typer.Option(False, "--force", help="Rebuild even if fresh"),
    no_deps: bool = typer.Option(
        False, "--no-deps", help="Build only this asset, never its stale ancestors"
    ),
    orchestrator: str = typer.Option(
        None,
        "--orchestrator",
        help="local | scheduler | prefect — overrides EXAMLOPS_ASSET_ORCHESTRATOR for this run",
    ),
) -> None:
    """Rebuild the asset + its stale ancestors only (R4/GWT-3).

    `--orchestrator scheduler` runs the build as a job on the phase-23 HPC scheduler (ADR 0036
    clause 3) — mock, Slurm or Flux — and waits for it, instead of running it in this process;
    `--orchestrator prefect` runs it as a Prefect flow run, visible in the Prefect UI (clause 1).
    """
    from examlops.assets import AssetBuildError
    from examlops.assets import materialize as _materialize

    try:
        result = _materialize(
            name, actor=_actor(), force=force, no_deps=no_deps, orchestrator=orchestrator
        )
    except AssetBuildError as exc:  # the build did not complete; no version was recorded
        _output.error(str(exc), hint="`exa hpc jobs` lists the job; its logs say why")
    if result.blocked:
        _output.error(f"Blocked by policy: {result.blocked}")
        return
    if _output.json_mode:
        _output.print_json(
            {"target": result.target, "rebuilt": result.rebuilt, "skipped": result.skipped}
        )
        return
    if result.rebuilt:
        _output.ok(f"Materialized {name}: rebuilt {', '.join(result.rebuilt)}")
    else:
        _output.info(f"{name} already fresh — nothing to rebuild.")
    if result.skipped:
        _output.info(f"  skipped (fresh): {', '.join(result.skipped)}")


@app.command("source-changed")
def source_changed(
    name: str = typer.Argument(..., help="Source (dataset) asset whose upstream data changed"),
) -> None:
    """Advance a source asset's version so downstream assets go stale (GWT-2)."""
    from examlops.assets import mark_source_changed

    v = mark_source_changed(name, actor=_actor())
    _output.ok(f"{name} advanced to v{v} — downstream assets are now stale.")


@app.command("graph")
def graph() -> None:
    """Print the asset DAG (coincides with the A2 lineage graph) (R6/GWT-4)."""
    from examlops.assets import build_dag

    dag = build_dag()
    if _output.json_mode:
        _output.print_json(dag)
        return
    if not dag:
        _output.info("No assets declared.")
        return
    for name, deps in dag.items():
        if deps:
            _output.info(f"{name} ← {', '.join(deps)}")
        else:
            _output.info(f"{name} (source)")
