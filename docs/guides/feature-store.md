# Feature Store & Train/Serve Consistency (A3)

> Next-Gen 40 · feature **A3** · ADR 0017 · spec `design/vision/specs/A3-feature-store.md`

A3 makes a **feature view** the single source of truth for a feature: the *same*
definition serves training (offline, point-in-time correct) and inference (online,
low-latency). Because there is only one definition, there is no second copy of the
transform to drift — **train/serve skew is zero by construction**.

The reference backend is [Feast](https://feast.dev) (MinIO offline store + Redis online
store). This platform implements the **same semantics in pure Python** over the shared
`platform.db`, so every command works — and every guarantee is testable — with no Feast or
Redis installed. When a Feast backend is configured the same `exa feature` commands drive
it.

## Concepts

| Term | Meaning |
|---|---|
| **Feature view** | A named, versioned definition: entity + feature list + TTL + revision |
| **Offline store** | Append-only event log of feature observations (point-in-time truth) |
| **Online store** | Latest materialized value per entity (low-latency serving read) |
| **Skew** | Difference between the value used at training and at serving for one entity |

## Define a view once

```bash
exa feature apply job_features --entity job \
    --features embedding,pclass,mbwidth --ttl 3600 --revision <A1-rev>
exa feature list
```

`--ttl` sets the freshness budget (seconds); `--revision` pins the view to an A1 dataset
revision for reproducibility (R4).

## Ingest offline observations

Each observation carries the entity's **event timestamp** — the point-in-time key:

```bash
exa feature ingest job_features --entity-id job-42 \
    --event-ts "2026-07-16 10:00:00" \
    --values '{"pclass": "compute-bound", "mbwidth": 42.0, "embedding": [/* 384-dim */]}'
```

Values outside the view's declared features are **projected away** — the view is the
contract (R1).

## Training retrieval is point-in-time correct

For a training set, each entity row provides its own event time; retrieval returns the
latest value **at or before** that time and never a later one (R4/R5) — no future leakage:

```bash
# The offline as-of value for an entity at a past event time:
exa feature get job_features --entity-id job-42 --asof "2026-07-16 12:00:00"
```

```python
from examlops.feature_store import get_training_features

rows = [{"entity_id": "job-42", "event_ts": "2026-07-16 12:00:00"}]
train_features = get_training_features("job_features", rows)  # as-of, no leakage
```

## Materialize → serve online

Materialization copies the latest offline value per entity into the online store:

```bash
exa feature materialize job_features
exa feature get job_features --entity-id job-42   # online (default) — low latency
```

## Zero skew (the guarantee)

After materialization the online value is *exactly* the latest offline as-of value — same
bytes, same vector:

```bash
exa feature skew job_features --entity-id job-42 --asof "2026-07-16 12:00:00"
# No skew for job-42 in job_features — online == offline as-of 2026-07-16 12:00:00.
```

`measure_skew` returns `matches=True` whenever the online store has been materialized past
the entity's event time — proving train and serve read one definition (R2 / GWT-1).

## Freshness monitoring

Materialization age is tracked and flagged stale past the view TTL (R6 / GWT-4), feeding
C5 data-quality:

```bash
exa feature freshness job_features
# job_features: materialized 2026-07-16 10:05:00 (age 120s, TTL 3600s) — fresh
```

## Graceful degradation

No Feast, no Redis, no MinIO required: the offline log, online snapshot, materialization
tracking, point-in-time retrieval, and skew/freshness checks are all pure Python over
`platform.db`. A Feast deployment is a drop-in that the same CLI targets.

## Related

- **A1** dataset revisions — a view (and its training sets) pin to a revision.
- **A2** lineage — feature views appear as lineage nodes.
- **B5** vector store — the `embedding` feature shares its definition for RAG.
- **C5** data quality & drift — materialization freshness feeds quality monitoring.
