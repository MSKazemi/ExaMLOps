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
    "  exa embedding status docs"
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
    """Register a versioned encoder → encoder_id (R1)."""
    from examlops.embeddings import register_encoder

    eid = register_encoder(name, version, dim, metric=metric, normalization=normalization)
    if _output.json_mode:
        _output.print_json({"encoder_id": eid, "name": name, "version": version, "dim": dim})
        return
    _output.ok(f"Registered encoder [bold]{eid}[/bold] ({name} {version}, dim {dim}, {metric})")


@app.command("list")
def list_cmd() -> None:
    """List registered encoders."""
    from examlops.platform_db import list_encoders

    encoders = list_encoders()
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
        ["Encoder ID", "Name", "Version", "Dim", "Metric", "Norm"],
        [
            [
                e["encoder_id"],
                e["name"],
                e["version"],
                str(e["dim"]),
                e["metric"],
                e["normalization"],
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
    from examlops.embeddings import set_collection_encoder
    from examlops.platform_db import get_encoder

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
) -> None:
    """Blue-green reindex to a new encoder — verified switch, old retained then pruned (R4/R5)."""
    from examlops.embeddings import reindex

    try:
        result = reindex(
            collection,
            new_encoder_id,
            tenant=tenant,
            corpus_size=corpus_size,
            recall_fn=lambda: recall,
            recall_floor=recall_floor,
            actor=_actor(),
        )
    except ValueError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
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
    """Show a collection's active/staging encoder + reindex history."""
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
