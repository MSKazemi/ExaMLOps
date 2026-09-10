"""B5 — `exa vector`: engine-agnostic vector store ops (ADR 0020)."""

from __future__ import annotations

import json

import typer

from examlops.cli import _output
from examlops.cli._enums import FusionMethod, VectorIndex, VectorSearchMode

app = typer.Typer(
    help="Vector store — collections, upsert, dense/sparse/hybrid search, reindex, stats, drop",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa vector create docs --dim 384 --metric cosine --index hnsw --m 16 --ef-search 64\n\n"
    "  exa vector upsert docs --id a1 --vector '[0.1, 0.2, ...]' --text 'JPCP job 4711 failed'\n\n"
    "  exa vector search docs --vector '[0.1, 0.2, ...]' -k 5 --filter '{\"lang\":\"en\"}'\n\n"
    "  exa vector search docs --vector '[0.1, ...]' --text 'job 4711' --mode hybrid\n\n"
    "  exa vector reindex docs --index ivfflat --lists 200 --probes 14\n\n"
    "  exa vector stats docs"
)

_INDEX_HELP = "ANN index: flat (exact scan) | hnsw | ivfflat"


def _index_config(
    index: VectorIndex | None,
    dim: int | None,
    m: int | None,
    ef_construction: int | None,
    ef_search: int | None,
    lists: int | None,
    probes: int | None,
):
    """Build an IndexConfig from CLI options, or ``None`` when no index option was given."""
    from examlops.vector_store import IndexConfig, IndexConfigError

    given = {
        "m": m,
        "ef_construction": ef_construction,
        "ef_search": ef_search,
        "lists": lists,
        "probes": probes,
    }
    if index is None and all(v is None for v in given.values()):
        return None
    if index is None:
        _output.error("index parameters need --index (hnsw | ivfflat)")
    try:
        return IndexConfig.build(str(index), dim=dim, **given)
    except IndexConfigError as exc:
        _output.error(str(exc))


def _json_arg(raw: str | None, what: str):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        _output.error(f"{what} is not valid JSON: {exc}")


@app.command("create", epilog=_EXAMPLES)
def create(
    collection: str = typer.Argument(..., help="Collection name"),
    dim: int = typer.Option(..., "--dim", help="Fixed dimensionality"),
    metric: str = typer.Option("cosine", "--metric", help="cosine | l2 | dot"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace (D6)"),
    encoder: str | None = typer.Option(
        None, "--encoder", help="Stamp the encoder that produces this collection's vectors"
    ),
    index: VectorIndex | None = typer.Option(None, "--index", help=_INDEX_HELP),
    m: int | None = typer.Option(None, "--m", help="HNSW links per node (2–100, default 16)"),
    ef_construction: int | None = typer.Option(
        None, "--ef-construction", help="HNSW build candidate list (>= 2·m, default 64)"
    ),
    ef_search: int | None = typer.Option(
        None, "--ef-search", help="HNSW query candidate list (1–1000, default 40)"
    ),
    lists: int | None = typer.Option(None, "--lists", help="IVFFlat lists (default 100)"),
    probes: int | None = typer.Option(None, "--probes", help="IVFFlat lists scanned per query"),
) -> None:
    """Create a vector collection with a fixed dim, distance metric and ANN index."""
    from examlops.vector_store import SchemaConflict, select_store

    cfg = _index_config(index, dim, m, ef_construction, ef_search, lists, probes)
    try:
        select_store().create_collection(
            collection, dim, metric, tenant, encoder_id=encoder, index=cfg
        )
    except (SchemaConflict, ValueError, RuntimeError) as exc:
        _output.error(str(exc))
    desc = cfg.describe() if cfg else "flat (exact scan)"
    _output.ok(
        f"Created collection '{collection}' (dim={dim}, metric={metric}, index={desc}, "
        f"tenant={tenant})"
    )


@app.command("upsert", epilog=_EXAMPLES)
def upsert(
    collection: str = typer.Argument(..., help="Collection name"),
    id: str = typer.Option(..., "--id", help="Item id"),
    vector: str = typer.Option(..., "--vector", help="JSON array of floats"),
    meta: str | None = typer.Option(None, "--meta", help="JSON metadata object"),
    text: str | None = typer.Option(
        None, "--text", help="Text for the sparse (BM25) channel of hybrid search"
    ),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace"),
    encoder: str | None = typer.Option(None, "--encoder", help="Encoder that made the vector"),
) -> None:
    """Upsert a single vector (rejected if dim mismatches the collection)."""
    from examlops.vector_store import (
        CollectionNotFound,
        DimensionMismatch,
        EncoderMismatch,
        VecItem,
        select_store,
    )

    vec = _json_arg(vector, "--vector")
    md = _json_arg(meta, "--meta") or {}
    try:
        select_store().upsert(collection, [VecItem(id, vec, md, text)], tenant, encoder_id=encoder)
    except (
        DimensionMismatch,
        EncoderMismatch,
        CollectionNotFound,
        ValueError,
        RuntimeError,
    ) as exc:
        _output.error(str(exc))
    _output.ok(f"Upserted '{id}' into '{collection}'")


@app.command("search", epilog=_EXAMPLES)
def search(
    collection: str = typer.Argument(..., help="Collection name"),
    vector: str | None = typer.Option(
        None, "--vector", help="JSON array of floats (query) — needed by dense and hybrid"
    ),
    text: str | None = typer.Option(
        None, "--text", help="Query text — needed by sparse and hybrid"
    ),
    mode: VectorSearchMode = typer.Option(
        VectorSearchMode.dense, "--mode", help="dense | sparse (BM25) | hybrid (both, fused)"
    ),
    fusion: FusionMethod = typer.Option(
        FusionMethod.rrf, "--fusion", help="Hybrid fusion: rrf (default) | convex"
    ),
    alpha: float = typer.Option(
        0.5, "--alpha", help="Convex fusion weight of the dense channel (0–1)"
    ),
    candidates: int | None = typer.Option(
        None, "--candidates", help="Per-channel results fused in hybrid mode (default max(4k,50))"
    ),
    k: int = typer.Option(5, "-k", "--k", help="Top-k results"),
    filter: str | None = typer.Option(None, "--filter", help="JSON metadata equality filter"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace"),
    encoder: str | None = typer.Option(None, "--encoder", help="Encoder that made the query"),
) -> None:
    """Top-k search: dense (metric), sparse (BM25) or hybrid (both, rank-fused)."""
    from examlops.vector_store import (
        CollectionNotFound,
        DimensionMismatch,
        EncoderMismatch,
        select_store,
    )

    if k < 1:
        _output.error("-k must be >= 1")
    vec = _json_arg(vector, "--vector")
    flt = _json_arg(filter, "--filter")
    if mode != VectorSearchMode.sparse and vec is None:
        _output.error(f"--mode {mode} needs --vector")
    if mode != VectorSearchMode.dense and not (text and text.strip()):
        _output.error(f"--mode {mode} needs --text")
    store = select_store()
    try:
        if mode == VectorSearchMode.dense:
            hits = store.search(collection, vec, k, flt, tenant, encoder_id=encoder)
        elif mode == VectorSearchMode.sparse:
            hits = store.sparse_search(collection, str(text), k, flt, tenant)
        else:
            hits = store.hybrid_search(
                collection,
                vec,
                str(text),
                k,
                flt,
                tenant,
                encoder_id=encoder,
                fusion=str(fusion),
                alpha=alpha,
                candidates=candidates,
            )
    except (DimensionMismatch, EncoderMismatch, CollectionNotFound, ValueError) as exc:
        _output.error(str(exc))
    if _output.json_mode:
        _output.print_json(
            [
                {"id": h.id, "score": h.score, "metadata": h.metadata, "channels": h.channels}
                for h in hits
            ]
        )
        return
    if not hits:
        _output.ok("No matches.")
        return
    if mode == VectorSearchMode.hybrid:

        def _rank(h, key: str) -> str:
            return str(int(h.channels[key])) if key in h.channels else "—"

        _output.print_table(
            f"Hybrid search: {collection} (top {k}, {fusion})",
            ["ID", "Fused", "Dense rank", "Sparse rank", "Metadata"],
            [
                [
                    h.id,
                    f"{h.score:.4f}",
                    _rank(h, "dense_rank"),
                    _rank(h, "sparse_rank"),
                    json.dumps(h.metadata),
                ]
                for h in hits
            ],
        )
        return
    _output.print_table(
        f"Search: {collection} (top {k}, {mode})",
        ["ID", "Score", "Metadata"],
        [[h.id, f"{h.score:.4f}", json.dumps(h.metadata)] for h in hits],
    )


@app.command("reindex", epilog=_EXAMPLES)
def reindex(
    collection: str = typer.Argument(..., help="Collection name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace"),
    index: VectorIndex | None = typer.Option(
        None, "--index", help=f"Switch to this index. {_INDEX_HELP}"
    ),
    m: int | None = typer.Option(None, "--m", help="HNSW links per node"),
    ef_construction: int | None = typer.Option(
        None, "--ef-construction", help="HNSW build candidate list"
    ),
    ef_search: int | None = typer.Option(None, "--ef-search", help="HNSW query candidate list"),
    lists: int | None = typer.Option(None, "--lists", help="IVFFlat lists"),
    probes: int | None = typer.Option(None, "--probes", help="IVFFlat lists scanned per query"),
) -> None:
    """Rebuild the collection index blue-green (search stays up); optionally change it."""
    from examlops.vector_store import CollectionNotFound, select_store

    store = select_store()
    dim = None
    if index is not None:
        try:
            dim = int(store.stats(collection, tenant)["dim"])
        except CollectionNotFound as exc:
            _output.error(str(exc))
    cfg = _index_config(index, dim, m, ef_construction, ef_search, lists, probes)
    try:
        store.reindex(collection, tenant, index=cfg)
    except (CollectionNotFound, ValueError, RuntimeError) as exc:
        _output.error(str(exc))
    suffix = f" → {cfg.describe()}" if cfg else ""
    _output.ok(f"Reindexed '{collection}'{suffix}")


@app.command("stats")
def stats(
    collection: str = typer.Argument(..., help="Collection name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace"),
) -> None:
    """Show collection dim, metric, index, how search is answered, and item count."""
    from examlops.vector_store import CollectionNotFound, IndexConfig, select_store

    store = select_store()
    try:
        s = store.stats(collection, tenant)
    except CollectionNotFound as exc:
        _output.error(str(exc))
    if _output.json_mode:
        _output.print_json(s)
        return
    idx = s.get("index") or {"type": "flat"}
    params = {k: v for k, v in idx.items() if k != "type"}
    desc = IndexConfig.build(idx["type"], **params).describe()
    _output.print_table(
        f"Collection: {collection}",
        ["Dim", "Metric", "Index", "Search", "Count", "Encoder", "Tenant"],
        [
            [
                str(s["dim"]),
                s["metric"],
                desc,
                str(s.get("search", "exact")),
                str(s["count"]),
                str(s.get("encoder_id") or "—"),
                s["tenant"],
            ]
        ],
    )


@app.command("drop")
def drop(
    collection: str = typer.Argument(..., help="Collection name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace"),
) -> None:
    """Delete a collection and every vector in it (irreversible; audited)."""
    from examlops.vector_store import CollectionNotFound, select_store

    store = select_store()
    try:
        n = int(store.stats(collection, tenant)["count"])
    except CollectionNotFound as exc:
        _output.error(str(exc))
    if not _output.confirm(
        f"Drop collection '{collection}' (tenant '{tenant}') and its {n} vectors? "
        "This cannot be undone."
    ):
        _output.info("Aborted.")
        raise typer.Exit(1)
    try:
        removed = store.drop_collection(collection, tenant)
    except CollectionNotFound as exc:
        _output.error(str(exc))
    try:
        from examlops.data.audit import write_audit_event

        write_audit_event(
            "exa-vector",
            None,
            "vector_collection_dropped",
            collection,
            {"items_removed": removed, "backend": store.name},
            tenant=tenant,
        )
    except Exception as exc:  # noqa: BLE001 - the drop happened; say the audit did not
        _output.warning(f"collection dropped but the audit event failed: {exc}")
    if _output.json_mode:
        _output.print_json({"collection": collection, "tenant": tenant, "removed": removed})
        return
    _output.ok(f"Dropped '{collection}' (tenant {tenant}): {removed} vectors removed")
