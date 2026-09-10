"""A3 — `exa feature`: feature store & train/serve consistency (ADR 0017).

One feature-view definition serves both training (offline, point-in-time) and inference
(online, low-latency), so there is no train/serve skew. Works with no Feast/Redis
installed (pure-Python over platform.db); the same commands drive a Feast backend when one
is configured.
"""

from __future__ import annotations

import json

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Feature store — one definition for train + serve, no skew (A3)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa feature apply job_features --entity job "
    "--features embedding,pclass,mbwidth --ttl 3600\n\n"
    "  exa feature list\n\n"
    "  exa feature ingest job_features --entity-id job-42 "
    '--event-ts "2026-07-16 10:00:00" --values \'{"pclass":"compute-bound"}\'\n\n'
    "  exa feature materialize job_features\n\n"
    "  exa feature apply job_features --entity job --features embedding,pclass --embedding embedding\n\n"
    "  exa feature similar job_features --entity-id job-42 -k 5\n\n"
    "  exa feature get job_features --entity-id job-42\n\n"
    "  exa feature freshness job_features"
)


@app.command("apply", epilog=_EXAMPLES)
def apply(
    name: str = typer.Argument(..., help="Feature view name"),
    entity: str = typer.Option(..., "--entity", help="Entity the view is keyed on"),
    features: str = typer.Option(..., "--features", help="Comma-separated feature names"),
    source: str = typer.Option(None, "--source", help="Offline source hint (parquet/table)"),
    ttl: int = typer.Option(0, "--ttl", help="Freshness TTL in seconds (0 = no staleness alert)"),
    dataset_revision: str = typer.Option(None, "--revision", help="A1 dataset revision pin"),
    embedding: str = typer.Option(
        None,
        "--embedding",
        help="Feature holding an embedding; materialize then indexes it for `exa feature similar`",
    ),
) -> None:
    """Register/patch a feature view — the single train+serve definition (R1)."""
    from examlops.feature_store import FeatureView, apply_view

    view = FeatureView(
        name=name,
        entity=entity,
        features=[f.strip() for f in features.split(",") if f.strip()],
        source=source,
        ttl_seconds=ttl,
        dataset_revision=dataset_revision,
        embedding_feature=embedding or None,
    )
    try:
        apply_view(view)
    except ValueError as exc:
        _output.error(str(exc))
    _output.ok(f"Applied feature view {name} on entity '{entity}' ({len(view.features)} features).")


@app.command("list")
def list_cmd() -> None:
    """List registered feature views."""
    from examlops.feature_store import list_views

    views = list_views()
    if _output.json_mode:
        _output.print_json(
            [
                {
                    "name": v.name,
                    "entity": v.entity,
                    "features": v.features,
                    "ttl_seconds": v.ttl_seconds,
                    "dataset_revision": v.dataset_revision,
                    "embedding_feature": v.embedding_feature,
                }
                for v in views
            ]
        )
        return
    if not views:
        _output.info("No feature views. Create one with: exa feature apply <name> --entity …")
        return
    _output.print_table(
        "Feature Views",
        ["Name", "Entity", "Features", "TTL(s)", "Revision", "Embedding"],
        [
            [
                v.name,
                v.entity,
                ", ".join(v.features),
                str(v.ttl_seconds),
                v.dataset_revision or "—",
                v.embedding_feature or "—",
            ]
            for v in views
        ],
    )


@app.command("ingest")
def ingest(
    view: str = typer.Argument(..., help="Feature view name"),
    entity_id: str = typer.Option(..., "--entity-id", help="Entity id"),
    event_ts: str = typer.Option(..., "--event-ts", help="Event timestamp (YYYY-MM-DD HH:MM:SS)"),
    values: str = typer.Option(..., "--values", help="JSON object of feature values"),
) -> None:
    """Record an offline feature observation (point-in-time source of truth)."""
    from examlops.feature_store import ingest as _ingest

    try:
        parsed = json.loads(values)
    except json.JSONDecodeError as exc:
        _output.error(f"--values is not valid JSON: {exc}")
        return
    _ingest(view, entity_id, event_ts, parsed)
    _output.ok(f"Ingested {entity_id} @ {event_ts} into {view}.")


@app.command("materialize")
def materialize(
    view: str = typer.Argument(..., help="Feature view name"),
    start: str = typer.Option(None, "--start", help="Window start timestamp"),
    end: str = typer.Option(None, "--end", help="Window end timestamp"),
) -> None:
    """Materialize latest offline values → online store (R6); index its embedding, if declared."""
    from examlops.feature_store import materialize_with_index

    out = materialize_with_index(view, start_ts=start, end_ts=end)
    idx = out["embeddings"]
    if _output.json_mode:
        _output.print_json(
            {
                "view": view,
                "rows": out["rows"],
                "embeddings": None
                if idx is None
                else {
                    "collection": idx.collection,
                    "indexed": idx.indexed,
                    "skipped": idx.skipped,
                    "dim": idx.dim,
                    "error": idx.error,
                    "skipped_reasons": idx.skipped_reasons,
                },
            }
        )
        return
    _output.ok(f"Materialized {out['rows']} entity row(s) for {view} to the online store.")
    if idx is None:
        return
    if idx.error:
        _output.warning(
            f"Embeddings not indexed into {idx.collection}: {idx.error} "
            "(the online store is materialized; re-run to index)"
        )
        return
    _output.ok(f"Indexed {idx.indexed} embedding(s) into {idx.collection} (dim {idx.dim}).")
    if idx.skipped:
        _output.warning(f"Skipped {idx.skipped} row(s): " + "; ".join(idx.skipped_reasons))


@app.command("get")
def get_cmd(
    view: str = typer.Argument(..., help="Feature view name"),
    entity_id: str = typer.Option(..., "--entity-id", help="Entity id"),
    asof: str = typer.Option(
        None, "--asof", help="Point-in-time (offline as-of) instead of the online value"
    ),
) -> None:
    """Read an entity's feature vector — online (default) or point-in-time offline (--asof)."""
    from examlops.feature_store import get_online_features, get_training_features

    if asof:
        vals = get_training_features(view, [{"entity_id": entity_id, "event_ts": asof}])[0]
        mode = f"offline as-of {asof}"
    else:
        vals = get_online_features(view, [entity_id])[0]
        mode = "online"
    if _output.json_mode:
        _output.print_json({"view": view, "entity_id": entity_id, "mode": mode, "values": vals})
        return
    if vals is None:
        _output.warning(f"No {mode} features for {entity_id} in {view}.")
        return
    _output.print_record({"view": view, "entity_id": entity_id, "mode": mode, **vals})


@app.command("skew")
def skew(
    view: str = typer.Argument(..., help="Feature view name"),
    entity_id: str = typer.Option(..., "--entity-id", help="Entity id"),
    asof: str = typer.Option(..., "--asof", help="Event timestamp to compare as-of"),
) -> None:
    """Assert online == offline as-of for an entity (skew must be zero) (R2/GWT-1)."""
    from examlops.feature_store import measure_skew

    result = measure_skew(view, entity_id, asof)
    if _output.json_mode:
        _output.print_json(result)
        return
    if result["matches"]:
        _output.ok(f"No skew for {entity_id} in {view} — online == offline as-of {asof}.")
    else:
        _output.warning(f"Skew detected for {entity_id} in {view}:")
        _output.info(f"  offline: {result['offline']}")
        _output.info(f"  online:  {result['online']}")


@app.command("freshness")
def freshness_cmd(
    view: str = typer.Argument(..., help="Feature view name"),
) -> None:
    """Show materialization age and staleness vs the view TTL (R6/GWT-4)."""
    from examlops.feature_store import freshness

    f = freshness(view)
    if _output.json_mode:
        _output.print_json(
            {
                "view": f.view,
                "materialized_at": f.materialized_at,
                "age_seconds": f.age_seconds,
                "ttl_seconds": f.ttl_seconds,
                "stale": f.stale,
            }
        )
        return
    if f.materialized_at is None:
        _output.warning(f"{view}: never materialized.")
        return
    age = f"{f.age_seconds:.0f}s" if f.age_seconds is not None else "unknown"
    line = (
        f"[bold]{view}[/bold]: materialized {f.materialized_at} (age {age}, TTL {f.ttl_seconds}s)"
    )
    if f.stale:
        _output.warning(line + " — STALE")
    else:
        _output.info(line + " — fresh")


@app.command("similar", epilog=_EXAMPLES)
def similar(
    view: str = typer.Argument(..., help="Feature view with an embedding feature"),
    entity_id: str = typer.Option(..., "--entity-id", help="Entity to find neighbours of"),
    k: int = typer.Option(5, "-k", "--k", help="How many neighbours"),
) -> None:
    """Entities whose embedding is nearest to this one's (ADR 0020 clause 4)."""
    from examlops.feature_store import similar_entities
    from examlops.vector_store import CollectionNotFound

    if k < 1:
        _output.error("-k must be >= 1")
    try:
        hits = similar_entities(view, entity_id, k)
    except (KeyError, ValueError, CollectionNotFound) as exc:
        _output.error(str(exc).strip("'\""))
    if _output.json_mode:
        _output.print_json(
            [{"entity_id": h.id, "score": h.score, "metadata": h.metadata} for h in hits]
        )
        return
    if not hits:
        _output.ok("No other entities indexed yet — run: exa feature materialize " + view)
        return
    _output.print_table(
        f"Nearest to {entity_id} in {view}",
        ["Entity", "Cosine similarity", "Materialized at"],
        [[h.id, f"{h.score:.4f}", str(h.metadata.get("event_ts", "—"))] for h in hits],
    )
