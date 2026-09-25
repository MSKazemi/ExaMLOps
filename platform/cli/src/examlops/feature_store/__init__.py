"""Next-Gen 40 · A3 — feature store & train/serve consistency (ADR 0017).

One feature definition (a *feature view*) serves both training (offline, point-in-time
correct) and inference (online, low-latency), eliminating train/serve skew. The reference
target is Feast (MinIO offline + Redis online); this module implements the **same
semantics in pure Python** over the shared ``platform.db`` so the platform works — and is
fully testable — with no Feast or Redis installed.

Guarantees:
- **Single definition (R1):** a feature view is registered once; training and serving both
  read it. There is no second copy of the transform to drift.
- **Zero skew (R2/GWT-1):** the online value materialized for an entity is *exactly* the
  latest offline value as of its event time — same bytes, same vector.
- **Point-in-time (R4/R5/GWT-2):** historical retrieval never reads a value stamped after
  the entity's event timestamp — no future leakage.
- **Revision pin (R4/GWT-3):** a training set is tagged with the A1 dataset revision it was
  built against.
- **Freshness (R6/GWT-4):** materialization age is monitored and flagged stale past the TTL.
- **Embedding features reach the vector store (ADR 0020 clause 4):** a view may name one of its
  features as its embedding; materializing the view then indexes that vector per entity into the
  ``features.<view>`` collection, so "entities like this one" is a nearest-neighbour query
  (:func:`similar_entities`) over exactly the values serving reads — one definition, no copy that
  can drift.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from examlops import data as platform_db


@dataclass
class FeatureView:
    name: str
    entity: str
    features: list[str]
    source: str | None = None
    ttl_seconds: int = 0
    dataset_revision: str | None = None
    #: Which declared feature holds an embedding (a list of floats). When set, materialization
    #: also indexes it into the ``features.<view>`` vector collection (ADR 0020 clause 4).
    embedding_feature: str | None = None
    #: The pack definition's typed specs + fingerprint (ADR 0017 clause 2), when synced from one.
    spec: dict[str, Any] | None = None
    #: > 0 puts the view on the materialization schedule (ADR 0017 clause 4).
    materialize_interval_seconds: int = 0


@dataclass
class EmbeddingIndexResult:
    """What one materialization did to the view's vector collection."""

    collection: str
    indexed: int = 0
    skipped: int = 0
    dim: int | None = None
    error: str | None = None
    skipped_reasons: list[str] = field(default_factory=list)


@dataclass
class Freshness:
    view: str
    materialized_at: str | None
    age_seconds: float | None
    ttl_seconds: int
    stale: bool


def apply_view(view: FeatureView) -> None:
    """Register/patch a feature view — the single definition for train + serve (R1).

    Also declares the view as an A4 asset with its source dataset upstream (ADR 0036), so the
    dependency graph gains the feature layer without anyone entering it twice. A view already
    names both halves of that edge — an entity-level `name` and the `source` it is built from —
    at exactly the granularity assets use, which is why this wiring is unambiguous where
    deriving assets from lineage events is not (see ADR 0036's note on granularity).
    """
    if view.embedding_feature and view.embedding_feature not in view.features:
        raise ValueError(
            f"embedding feature '{view.embedding_feature}' is not one of the view's features "
            f"{view.features}"
        )
    platform_db.upsert_feature_view(
        view.name,
        view.entity,
        view.features,
        source=view.source,
        ttl_seconds=view.ttl_seconds,
        dataset_revision=view.dataset_revision,
        embedding_feature=view.embedding_feature,
        spec=view.spec,
        materialize_interval_seconds=view.materialize_interval_seconds,
    )
    _declare_feature_asset(view)


def _declare_feature_asset(view: FeatureView) -> None:
    """Declare the view as a `feature` asset depending on its source dataset. Best-effort.

    Re-applying a view with the same source is idempotent, and changing the source is a genuine
    definition change that *should* move the edge — so unlike a per-run derivation, the deps here
    cannot flip-flop between runs.

    The view definition is the durable fact; the asset graph is a derived view of it. Failing to
    update the graph must never lose the definition.
    """
    try:
        from examlops.assets import declare_asset

        declare_asset(
            view.name,
            kind="feature",
            deps=[view.source] if view.source else [],
            description=f"feature view over {view.source or 'an unnamed source'}",
        )
    except Exception:
        pass


def get_view(name: str) -> FeatureView | None:
    d = platform_db.get_feature_view(name)
    if not d:
        return None
    return FeatureView(
        name=d["name"],
        entity=d["entity"],
        features=d["features"],
        source=d.get("source"),
        ttl_seconds=d.get("ttl_seconds", 0),
        dataset_revision=d.get("dataset_revision"),
        embedding_feature=d.get("embedding_feature"),
        spec=d.get("spec"),
        materialize_interval_seconds=int(d.get("materialize_interval_seconds") or 0),
    )


def list_views() -> list[FeatureView]:
    return [
        FeatureView(
            name=d["name"],
            entity=d["entity"],
            features=d["features"],
            source=d.get("source"),
            ttl_seconds=d.get("ttl_seconds", 0),
            dataset_revision=d.get("dataset_revision"),
            embedding_feature=d.get("embedding_feature"),
            spec=d.get("spec"),
            materialize_interval_seconds=int(d.get("materialize_interval_seconds") or 0),
        )
        for d in platform_db.list_feature_views()
    ]


def ingest(view: str, entity_id: str, event_ts: str, values: dict[str, Any]) -> None:
    """Record an offline feature observation (the point-in-time source of truth)."""
    platform_db.write_feature_record(view, entity_id, event_ts, values)


def _project(view: str, values: dict[str, Any] | None) -> dict[str, Any] | None:
    """Restrict a value dict to the view's declared features (stable order)."""
    if values is None:
        return None
    vw = platform_db.get_feature_view(view)
    if not vw:
        return values
    feats = vw["features"]
    return {k: values.get(k) for k in feats}


def get_training_features(
    view: str, entity_rows: list[dict[str, Any]]
) -> list[dict[str, Any] | None]:
    """Point-in-time (as-of) retrieval for training (R4/R5).

    ``entity_rows`` = ``[{"entity_id": ..., "event_ts": ...}, ...]``. For each row the
    latest offline value **at or before** ``event_ts`` is returned — never a later one.
    """
    from examlops.data.data_assets import get_offline_features_asof_many

    found = get_offline_features_asof_many(
        view, [(str(r["entity_id"]), str(r["event_ts"])) for r in entity_rows]
    )
    # One registry read for the whole batch, not one per row.
    vw = platform_db.get_feature_view(view) if any(v is not None for v in found) else None
    feats = vw["features"] if vw else None
    return [
        None if v is None else ({k: v.get(k) for k in feats} if feats is not None else v)
        for v in found
    ]


def get_online_features(view: str, entity_ids: list[str]) -> list[dict[str, Any] | None]:
    """Low-latency online read for serving (R3) — through the configured online store.

    With the Redis tier selected (ADR 0017 clause 1) a Redis miss or error falls back to the
    durable table, so this never returns less than the table holds.
    """
    from examlops.feature_store.online import select_online_store

    store = select_online_store()
    return [_project(view, store.read(view, e)) for e in entity_ids]


def materialize(view: str, *, start_ts: str | None = None, end_ts: str | None = None) -> int:
    """Materialize latest offline values → online store (R6). Returns rows written.

    A view with an ``embedding_feature`` also has its embeddings indexed (see
    :func:`materialize_with_index`, which reports what the index step did).
    """
    return int(materialize_with_index(view, start_ts=start_ts, end_ts=end_ts)["rows"])


def embedding_collection(view: str) -> str:
    """The vector collection a view's embedding feature is indexed into."""
    return f"features.{view}"


def _as_vector(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or not value:
        return None
    try:
        vec = [float(x) for x in value]
    except (TypeError, ValueError):
        return None
    return vec if all(math.isfinite(x) for x in vec) else None


def index_embeddings(view: str, *, store: Any = None) -> EmbeddingIndexResult:
    """Index every materialized entity's embedding into ``features.<view>`` (ADR 0020 clause 4).

    Reads the **online** rows — the values serving reads — so the index can never disagree with
    serving. Rows whose embedding is missing, non-numeric, non-finite or of another dimension are
    skipped and counted, never silently coerced: a zero-filled vector would score as a real
    neighbour. The collection is created on first use with the first valid vector's dimension and
    cosine distance; a later dimension change is refused by the store (``SchemaConflict``).
    """
    from examlops.vector_store import CollectionNotFound, select_store

    vw = platform_db.get_feature_view(view)
    if not vw:
        raise KeyError(f"feature view '{view}' not found")
    feature = vw.get("embedding_feature")
    coll = embedding_collection(view)
    result = EmbeddingIndexResult(collection=coll)
    if not feature:
        return result
    from examlops.data.data_assets import iter_online_features  # the owned per-domain module

    store = store or select_store()
    try:
        dim = int(store.stats(coll, "default")["dim"])
    except CollectionNotFound:
        dim = None
    created = False
    for page in iter_online_features(view):  # one page of vectors in memory at a time
        items = _index_items(view, feature, page, dim, result)
        if not items:
            continue
        dim = len(items[0].vector)
        if not created:
            store.create_collection(coll, int(dim), "cosine", "default")
            created = True
        store.upsert(coll, items, "default")
        result.indexed += len(items)
    result.dim = dim
    return result


def _index_items(
    view: str, feature: str, rows: list[dict[str, Any]], dim: int | None, result: Any
) -> list[Any]:
    """The indexable vectors of one page (all of ``dim`` when known); skips counted on result."""
    from examlops.vector_store import VecItem

    items: list[VecItem] = []
    for row in rows:
        vec = _as_vector(row["values"].get(feature))
        if vec is None:
            result.skipped += 1
            if len(result.skipped_reasons) < 5:
                result.skipped_reasons.append(f"{row['entity_id']}: no usable '{feature}' vector")
            continue
        if dim is None:
            dim = len(vec)
        if len(vec) != dim:
            result.skipped += 1
            if len(result.skipped_reasons) < 5:
                result.skipped_reasons.append(
                    f"{row['entity_id']}: dim {len(vec)} != collection dim {dim}"
                )
            continue
        items.append(
            VecItem(row["entity_id"], vec, {"view": view, "event_ts": str(row["event_ts"])})
        )
    return items


def materialize_with_index(
    view: str, *, start_ts: str | None = None, end_ts: str | None = None
) -> dict[str, Any]:
    """Materialize, then index the view's embeddings. ``{"rows": n, "embeddings": result|None}``.

    The online store is the durable fact and the vector index is derived from it, so an indexing
    failure (say, an unreachable pgvector) is reported in the result — never raised, and never
    allowed to undo a materialization that succeeded.

    With a serving tier configured (Redis, ADR 0017 clause 1) the result also carries
    ``"online"``: a :class:`~examlops.feature_store.online.MirrorResult` for the mirror write.
    """
    rows = platform_db.materialize_online(view, start_ts=start_ts, end_ts=end_ts)
    vw = platform_db.get_feature_view(view) or {}
    mirror = _mirror_online(view, int(vw.get("ttl_seconds") or 0))
    out: dict[str, Any] = {"rows": rows, "embeddings": None}
    if mirror.backend != "db":
        out["online"] = mirror
    if not vw.get("embedding_feature"):
        return out
    try:
        out["embeddings"] = index_embeddings(view)
    except Exception as exc:  # noqa: BLE001 - reported, not raised (see docstring)
        out["embeddings"] = EmbeddingIndexResult(
            collection=embedding_collection(view), error=f"{type(exc).__name__}: {exc}"
        )
    return out


def _mirror_online(view: str, ttl_seconds: int) -> Any:
    """Mirror the durable online rows into the serving tier (ADR 0017 clause 1). Never raises.

    A no-op for the default durable-table store. With Redis selected, a failure is reported in
    the result — the durable materialization already succeeded and stays.
    """
    from examlops.feature_store.online import MirrorResult, select_online_store

    store = select_online_store()
    if store.backend == "db":
        return MirrorResult(backend="db")
    total = MirrorResult(backend=store.backend)
    try:
        from examlops.data.data_assets import iter_online_features

        # Page by page: the whole view is never held in memory at once.
        for page in iter_online_features(view):
            if total.error is None:
                part = store.write(view, page, ttl_seconds=ttl_seconds)
                total.written += part.written
                total.invalidated += part.invalidated
                total.error = part.error
                continue
            # A page after a failed one was never rewritten either: its keys would keep serving
            # superseded values, so they are invalidated like the failed page's remainder.
            invalidate = getattr(store, "invalidate", None)
            if invalidate is None or not invalidate(view, page, total):
                break
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        total.error = f"{type(exc).__name__}: {exc}"
    return total


def ingest_rows(view: str, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Append ``[{"entity_id", "event_ts", "values"}]`` to the offline store, idempotently.

    An observation already recorded for the same ``(view, entity_id, event_ts)`` is skipped, so
    re-running a training gate over the same pinned data does not duplicate the offline log.
    Returns ``{"written", "skipped"}``. The batch is one transaction.
    """
    from examlops.data.data_assets import write_feature_records_once

    return write_feature_records_once(view, rows)


def similar_entities(view: str, entity_id: str, k: int = 5) -> list[Any]:
    """The ``k`` entities whose embedding is nearest to ``entity_id``'s (itself excluded).

    The query vector is the entity's **online** embedding, the same value serving would use.
    """
    from examlops.vector_store import select_store

    vw = platform_db.get_feature_view(view)
    if not vw or not vw.get("embedding_feature"):
        raise ValueError(f"feature view '{view}' declares no embedding feature")
    online = platform_db.get_online_feature(view, entity_id)
    vec = _as_vector((online or {}).get(vw["embedding_feature"]))
    if vec is None:
        raise KeyError(f"entity '{entity_id}' has no materialized embedding in '{view}'")
    hits = select_store().search(embedding_collection(view), vec, k + 1, None, "default")
    return [h for h in hits if h.id != entity_id][:k]


def measure_skew(view: str, entity_id: str, asof_ts: str) -> dict[str, Any]:
    """Compare the online value vs the offline as-of value for an entity (R2/GWT-1).

    ``skew == 0`` (``matches=True``) whenever the online store has been materialized past
    the entity's event time — proving train and serve read one definition.
    """
    offline = _project(view, platform_db.get_offline_features_asof(view, entity_id, asof_ts))
    online = _project(view, platform_db.get_online_feature(view, entity_id))
    return {
        "view": view,
        "entity_id": entity_id,
        "offline": offline,
        "online": online,
        "matches": offline == online,
    }


def _parse_ts(ts: str) -> float | None:
    from datetime import datetime

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts[: len(fmt) + 2], fmt).timestamp()
        except (ValueError, TypeError):
            continue
    return None


def freshness(view: str, *, now_ts: str | None = None) -> Freshness:
    """Report materialization age and staleness vs the view TTL (R6/GWT-4).

    ``now_ts`` is injectable for testing; if omitted the current wall clock is used.
    """
    vw = platform_db.get_feature_view(view)
    ttl = vw["ttl_seconds"] if vw else 0
    last = platform_db.last_materialization(view)
    if not last or not last.get("materialized_at"):
        return Freshness(view, None, None, ttl, stale=True)
    mat_ts = last["materialized_at"]
    if now_ts is not None:
        now = _parse_ts(now_ts)
    else:
        from datetime import datetime

        now = datetime.now(UTC).timestamp()
    mat = _parse_ts(mat_ts)
    if now is None or mat is None:
        return Freshness(view, mat_ts, None, ttl, stale=False)
    age = max(0.0, now - mat)
    stale = ttl > 0 and age > ttl
    return Freshness(view, mat_ts, age, ttl, stale)


__all__ = [
    "FeatureView",
    "Freshness",
    "apply_view",
    "get_view",
    "list_views",
    "ingest",
    "ingest_rows",
    "get_training_features",
    "get_online_features",
    "materialize",
    "measure_skew",
    "freshness",
]
