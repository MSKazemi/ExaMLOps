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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import Any

from examlops import platform_db


@dataclass
class FeatureView:
    name: str
    entity: str
    features: list[str]
    source: str | None = None
    ttl_seconds: int = 0
    dataset_revision: str | None = None


@dataclass
class Freshness:
    view: str
    materialized_at: str | None
    age_seconds: float | None
    ttl_seconds: int
    stale: bool


def apply_view(view: FeatureView) -> None:
    """Register/patch a feature view — the single definition for train + serve (R1)."""
    platform_db.upsert_feature_view(
        view.name,
        view.entity,
        view.features,
        source=view.source,
        ttl_seconds=view.ttl_seconds,
        dataset_revision=view.dataset_revision,
    )


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
    out: list[dict[str, Any] | None] = []
    for row in entity_rows:
        vals = platform_db.get_offline_features_asof(view, row["entity_id"], row["event_ts"])
        out.append(_project(view, vals))
    return out


def get_online_features(view: str, entity_ids: list[str]) -> list[dict[str, Any] | None]:
    """Low-latency online read for serving (R3)."""
    return [_project(view, platform_db.get_online_feature(view, e)) for e in entity_ids]


def materialize(view: str, *, start_ts: str | None = None, end_ts: str | None = None) -> int:
    """Materialize latest offline values → online store (R6). Returns rows written."""
    return platform_db.materialize_online(view, start_ts=start_ts, end_ts=end_ts)


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
    "get_training_features",
    "get_online_features",
    "materialize",
    "measure_skew",
    "freshness",
]
