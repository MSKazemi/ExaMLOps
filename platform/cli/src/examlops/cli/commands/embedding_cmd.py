"""B6 — `exa embedding`: encoder lifecycle & blue-green reindexing (ADR 0043).

Register versioned encoders, guard against cross-encoder comparisons, and reindex a
collection to a new encoder with verified blue-green switching. Governs the embeddings used
by the B3 cache / B4 RAG / B5 vector store.
"""

from __future__ import annotations

import os

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Embedding lifecycle — encoders + blue-green reindex (B6)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa embedding register nomic-embed-text v1.5 --dim 768\n\n"
    "  exa embedding list\n\n"
    "  exa embedding set-encoder docs <encoder-id>\n\n"
    "  exa embedding reindex docs <new-encoder-id> --corpus-size 10000 --recall 0.97\n\n"
    "  exa embedding status docs\n\n"
    "  EXAMLOPS_ENCODER_REGISTRY=mlflow exa embedding migrate --dry-run"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


@app.command("register", epilog=_EXAMPLES)
def register(
    name: str = typer.Argument(..., help="Encoder name (e.g. nomic-embed-text)"),
    version: str = typer.Argument(..., help="Encoder version (e.g. v1.5)"),
    dim: int = typer.Option(..., "--dim", help="Embedding dimension"),
    metric: str = typer.Option("cosine", "--metric", help="cosine | dot | l2"),
    normalization: str = typer.Option("l2", "--norm", help="Normalization (l2/none)"),
) -> None:
    """Register a versioned encoder → encoder_id (R1).

    With `EXAMLOPS_ENCODER_REGISTRY=mlflow` the encoder is published to the MLflow encoder
    registry first (ADR 0043 clause 1); re-registering a local-only encoder publishes it.
    """
    from examlops.embeddings import register_encoder
    from examlops.embeddings.mlflow_registry import EncoderRegistryError

    try:
        eid = register_encoder(
            name, version, dim, metric=metric, normalization=normalization, actor=_actor()
        )
    except (ValueError, EncoderRegistryError) as exc:
        _output.error(str(exc))
    if _output.json_mode:
        _output.print_json({"encoder_id": eid, "name": name, "version": version, "dim": dim})
        return
    _output.ok(f"Registered encoder {eid} ({name} {version}, dim {dim}, {metric})")


@app.command("list")
def list_cmd() -> None:
    """List registered encoders — from the registry of record (local, or MLflow)."""
    from examlops.embeddings import list_encoders
    from examlops.embeddings.mlflow_registry import EncoderRegistryError

    try:
        encoders = list_encoders()
    except (ValueError, EncoderRegistryError) as exc:
        _output.error(str(exc))
    if _output.json_mode:
        _output.print_json(encoders)
        return
    if not encoders:
        _output.info(
            "No encoders. Register one with: exa embedding register <name> <version> --dim N"
        )
        return
    _output.print_table(
        "Encoders",
        ["Encoder ID", "Name", "Version", "Dim", "Metric", "Norm", "Registry"],
        [
            [
                e["encoder_id"],
                e["name"],
                e["version"],
                str(e["dim"]),
                e["metric"],
                e["normalization"],
                e.get("registry", "local"),
            ]
            for e in encoders
        ],
    )


@app.command("set-encoder")
def set_encoder(
    collection: str = typer.Argument(..., help="Collection name"),
    encoder_id: str = typer.Argument(..., help="Encoder id"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Bootstrap a collection's active encoder (R2)."""
    from examlops.embeddings import get_encoder, set_collection_encoder

    if get_encoder(encoder_id) is None:
        _output.error(f"Unknown encoder {encoder_id} — register it first.")
        raise typer.Exit(1)
    set_collection_encoder(collection, encoder_id, tenant=tenant)
    _output.ok(f"Collection {collection} active encoder → {encoder_id}")


@app.command("reindex")
def reindex_cmd(
    collection: str = typer.Argument(..., help="Collection name"),
    new_encoder_id: str = typer.Argument(..., help="New encoder id"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
    corpus_size: int = typer.Option(0, "--corpus-size", help="Docs to re-embed"),
    recall: float = typer.Option(1.0, "--recall", help="Measured recall of the new index"),
    recall_floor: float = typer.Option(0.9, "--recall-floor", help="Minimum recall to switch"),
    inline: bool = typer.Option(
        False, "--inline", help="Run here even if EXAMLOPS_REINDEX_ORCHESTRATOR=scheduler"
    ),
    scheduler: bool = typer.Option(
        False, "--scheduler", help="Submit to the HPC scheduler instead of running here"
    ),
) -> None:
    """Blue-green reindex to a new encoder — verified switch, old retained then pruned (R4/R5).

    Large corpora belong on the scheduler (ADR 0043 clause 4): `--scheduler` runs the reindex as
    a job (mock / Slurm / Flux) carrying `--recall` / `--recall-floor`, and returns once it is
    queued; `exa embedding status` follows the same reindex row to its outcome. `--inline` forces
    the local path.
    """
    from examlops.embeddings import ReindexSubmissionError, reindex

    if inline and scheduler:
        _output.error("--inline and --scheduler are mutually exclusive.")
    mode = "inline" if inline else ("scheduler" if scheduler else None)

    try:
        result = reindex(
            collection,
            new_encoder_id,
            tenant=tenant,
            corpus_size=corpus_size,
            recall=recall,
            recall_floor=recall_floor,
            orchestrator=mode,
            actor=_actor(),
        )
    except (ValueError, ReindexSubmissionError) as exc:
        _output.error(str(exc))
    if _output.json_mode:
        _output.print_json(
            {
                "collection": result.collection,
                "from_encoder": result.from_encoder,
                "to_encoder": result.to_encoder,
                "recall": result.recall,
                "switched": result.switched,
                "reason": result.reason,
            }
        )
        return
    if result.switched:
        _output.ok(
            f"Reindexed {collection} → {new_encoder_id} (recall {result.recall:.3f}) — {result.reason}"
        )
    else:
        _output.warning(f"Reindex NOT switched: {result.reason}")


@app.command("status")
def status(
    collection: str = typer.Argument(..., help="Collection name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Show a collection's active/staging encoder + reindex history.

    A reindex still `submitted` whose scheduler job has ended without settling it is marked
    `failed` first (and audited), so a dead job never reads as queued. Only a clear terminal
    answer from the scheduler settles a row; anything else leaves it as it is.
    """
    from examlops.embeddings import reindex_status

    result = reindex_status(collection, tenant)
    coll = result["collection"]
    if _output.json_mode:
        _output.print_json(result)
        return
    if not coll:
        _output.info(f"No collection {collection}.")
        return
    _output.print_record(
        {
            "collection": collection,
            "active_encoder": coll["active_encoder_id"] or "—",
            "staging_encoder": coll["staging_encoder_id"] or "—",
            "status": coll["status"],
        }
    )
    if result["jobs"]:
        _output.print_table(
            "Reindex Jobs",
            ["To Encoder", "Status", "Recall", "Docs"],
            [
                [
                    j["to_encoder"],
                    j["status"],
                    f"{j['recall']:.3f}" if j["recall"] is not None else "—",
                    str(j["docs_reindexed"]),
                ]
                for j in result["jobs"]
            ],
        )


@app.command("migrate")
def migrate(
    dry_run: bool = typer.Option(False, "--dry-run", help="List what would be published"),
) -> None:
    """Publish every locally registered encoder to the MLflow encoder registry (ADR 0043 cl. 1).

    Additive and idempotent: encoder ids are content-addressed, so one already in MLflow is
    skipped as the same record. Works before switching `EXAMLOPS_ENCODER_REGISTRY=mlflow`, so a
    deployment can publish first and switch after. Needs `MLFLOW_TRACKING_URI`.
    """
    from examlops.embeddings import migrate_encoders
    from examlops.embeddings.mlflow_registry import EncoderRegistryError

    try:
        report = migrate_encoders(dry_run=dry_run, actor=_actor())
    except EncoderRegistryError as exc:
        _output.error(str(exc))
    if _output.json_mode:
        _output.print_json(report)
        return
    verb = "Would publish" if dry_run else "Published"
    _output.ok(
        f"{verb} {len(report['published'])} encoder(s) to MLflow; "
        f"{len(report['skipped'])} already there."
    )
