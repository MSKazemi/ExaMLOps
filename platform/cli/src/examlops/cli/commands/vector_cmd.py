"""B5 — `exa vector`: engine-agnostic vector store ops (ADR 0020)."""

from __future__ import annotations

import json

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Vector store — collections, upsert, search, reindex, stats",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa vector create docs --dim 384 --metric cosine\n\n"
    "  exa vector upsert docs --id a1 --vector '[0.1, 0.2, ...]' --meta '{\"lang\":\"en\"}'\n\n"
    "  exa vector search docs --vector '[0.1, 0.2, ...]' -k 5 --filter '{\"lang\":\"en\"}'\n\n"
    "  exa vector stats docs"
)


@app.command("create", epilog=_EXAMPLES)
def create(
    collection: str = typer.Argument(..., help="Collection name"),
    dim: int = typer.Option(..., "--dim", help="Fixed dimensionality"),
    metric: str = typer.Option("cosine", "--metric", help="cosine | l2 | dot"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace (D6)"),
) -> None:
    """Create a vector collection with a fixed dim + distance metric."""
    from examlops.vector_store import select_store

    try:
        select_store().create_collection(collection, dim, metric, tenant)
    except ValueError as exc:
        _output.error(str(exc))
    _output.ok(f"Created collection '{collection}' (dim={dim}, metric={metric}, tenant={tenant})")


@app.command("upsert", epilog=_EXAMPLES)
def upsert(
    collection: str = typer.Argument(..., help="Collection name"),
    id: str = typer.Option(..., "--id", help="Item id"),
    vector: str = typer.Option(..., "--vector", help="JSON array of floats"),
    meta: str | None = typer.Option(None, "--meta", help="JSON metadata object"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace"),
) -> None:
    """Upsert a single vector (rejected if dim mismatches the collection)."""
    from examlops.vector_store import CollectionNotFound, DimensionMismatch, VecItem, select_store

    try:
        vec = json.loads(vector)
        md = json.loads(meta) if meta else {}
        select_store().upsert(collection, [VecItem(id, vec, md)], tenant)
    except (DimensionMismatch, CollectionNotFound, ValueError) as exc:
        _output.error(str(exc))
    _output.ok(f"Upserted '{id}' into '{collection}'")


@app.command("search", epilog=_EXAMPLES)
def search(
    collection: str = typer.Argument(..., help="Collection name"),
    vector: str = typer.Option(..., "--vector", help="JSON array of floats (query)"),
    k: int = typer.Option(5, "-k", "--k", help="Top-k results"),
    filter: str | None = typer.Option(None, "--filter", help="JSON metadata equality filter"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace"),
) -> None:
    """Search top-k nearest by the collection metric, with optional metadata filter."""
    from examlops.vector_store import CollectionNotFound, DimensionMismatch, select_store

    try:
        vec = json.loads(vector)
        flt = json.loads(filter) if filter else None
        hits = select_store().search(collection, vec, k, flt, tenant)
    except (DimensionMismatch, CollectionNotFound, ValueError) as exc:
        _output.error(str(exc))
    if _output.json_mode:
        _output.print_json([{"id": h.id, "score": h.score, "metadata": h.metadata} for h in hits])
        return
    if not hits:
        _output.ok("No matches.")
        return
    _output.print_table(
        f"Search: {collection} (top {k})",
        ["ID", "Score", "Metadata"],
        [[h.id, f"{h.score:.4f}", json.dumps(h.metadata)] for h in hits],
    )


@app.command("reindex")
def reindex(
    collection: str = typer.Argument(..., help="Collection name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace"),
) -> None:
    """Rebuild the collection index (blue-green; recall preserved) — invoked by B6."""
    from examlops.vector_store import CollectionNotFound, select_store

    try:
        select_store().reindex(collection, tenant)
    except CollectionNotFound as exc:
        _output.error(str(exc))
    _output.ok(f"Reindexed '{collection}'")


@app.command("stats")
def stats(
    collection: str = typer.Argument(..., help="Collection name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace"),
) -> None:
    """Show collection dim, metric, and item count."""
    from examlops.vector_store import CollectionNotFound, select_store

    store = select_store()
    try:
        s = store.stats(collection, tenant)  # type: ignore[attr-defined]
    except CollectionNotFound as exc:
        _output.error(str(exc))
    if _output.json_mode:
        _output.print_json(s)
        return
    _output.print_table(
        f"Collection: {collection}",
        ["Dim", "Metric", "Count", "Tenant"],
        [[str(s["dim"]), s["metric"], str(s["count"]), s["tenant"]]],
    )
