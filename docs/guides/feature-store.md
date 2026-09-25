# Feature Store & Train/Serve Consistency (A3)

> Next-Gen 40 · feature **A3** · ADR 0017 · spec `design/vision/specs/A3-feature-store.md`

A3 makes a **feature view** the single source of truth for a feature: the *same*
definition serves training (offline, point-in-time correct) and inference (online,
low-latency). Because there is only one definition, there is no second copy of the
transform to drift — **train/serve skew is zero by construction**.

This is a bespoke pure-Python implementation of [Feast](https://feast.dev)'s *contract* —
versioned views, an offline point-in-time store, an online store, skew measurement — over the
shared `platform.db`, not Feast itself (ADR 0017 records why). It needs no Feast server. Redis is
optional: `EXAMLOPS_FEATURE_ONLINE_STORE=redis` puts a Redis serving tier in front of the durable
online table (see [Redis online store](#redis-online-store)).

Where the definition lives, and who enforces it:

| Side | What reads the definition | What it does with it |
|---|---|---|
| Use-case pack | `usecases/<pack>/features/*.yaml` | The single source of truth, one view per file |
| Training | the **feature gate** in `training_flow` | Validates the pinned training data with the transform serving uses; fails the run closed |
| Serving | `FeatureTransformer` in the inference pipeline | Transforms every request with that transform; can look an entity's features up online |
| Registry | `exa feature sync` | Mirrors the files into `feature_views` for the CLI, the scheduler and the dashboard |
| Scheduler | control plane, `exa feature materialize-due` | Re-materializes views on their `materialize_interval_seconds` |

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

## Embedding features → nearest neighbours

A view can name one of its features as its **embedding**, a list of floats such as FData's 384-dim
job embedding. Materializing the view then also indexes each entity's online embedding into the
vector collection `features.<view>`, and `exa feature similar` finds the entities nearest to one:

```bash
exa feature apply job_features --entity job --features embedding,pclass,mbwidth --embedding embedding
exa feature materialize job_features
# ✓ Materialized 1200 entity row(s) for job_features to the online store.
# ✓ Indexed 1198 embedding(s) into features.job_features (dim 384).
# ⚠ Skipped 2 row(s): job-17: no usable 'embedding' vector; …
exa feature similar job_features --entity-id job-42 -k 5     # cosine similarity
```

- The index is built from the **online** rows, the values serving reads, so it can never disagree
  with serving. Re-materializing updates an entity's vector.
- A row whose embedding is missing, non-numeric, non-finite, or of another dimension is
  **skipped and counted**, never coerced: a zero-filled vector would score as a real neighbour.
- The online store is the durable fact and the index is derived from it. If indexing fails (say
  pgvector is unreachable), materialization still succeeds and the failure is reported;
  re-run to index.
- The collection uses the [vector store](vector-store.md) backend in force (`sqlite` or
  `pgvector`), so it gets that store's dimension checks, index configuration and hybrid search.
  A view with no embedding feature materializes exactly as before.

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

## Declare a view in the use-case pack

A pack keeps its feature views next to its models, one per file. The reference pack declares the
F-DATA job embedding its FData-trained models consume
(`usecases/reference/features/fdata_job.yaml`):

```yaml
name: fdata_job_features
entity: job
entity_key: job_id          # inference-request field naming the entity
entity_column: jid          # training-data column naming it (defaults to entity_key)
timestamp_field: adt        # dataset column giving each row's event time
source: FDataDataset
serving: true               # the view FeatureTransformer applies by default
embedding_feature: embedding
ttl_seconds: 86400
materialize_interval_seconds: 3600
features:
  - name: embedding
    dtype: vector           # vector | float | int | str | bool
    dim: 384                # required for a vector, exact
    required: true
```

A model YAML binds a dataset entry to it with `feature_view:`:

```yaml
datasets:
  - name: FDataDataset
    input_features: [embedding]
    feature_view: fdata_job_features
```

`exa feature sync` mirrors every definition into the registry. It is idempotent (an unchanged view
reports `unchanged`), audits every change as `feature_view_synced`, refuses the whole set while any
file is malformed, and never deletes a registered view the pack does not declare (it is listed as
an orphan). The *fingerprint*, a sha256 over the name, entity, typed features and embedding
feature, identifies a definition; changing a TTL or a schedule does not change it.

`EXAMLOPS_FEATURES_DIR` points at a different definitions directory.

## The training gate

`training_flow` runs the **feature gate** after the data-contract gate, for every dataset entry
with a `feature_view:` binding. It reads the same bounded sample the contract gate reads (the data
this run trains on) and:

1. refuses the run if a required feature column is missing, or if any sampled row fails the
   transform (wrong dimension, non-numeric, non-finite, missing);
2. when the view names its entity column (`entity_column`, else `entity_key`) and
   `timestamp_field` and both are columns, appends the validated rows to the offline store in one
   transaction (event times converted to UTC; an entity and event time it already holds is
   skipped, so a re-run is idempotent), reads each back with point-in-time retrieval, and refuses
   the run if any differs from what the model trains on — the offline store already holding a
   different value for that entity and time. An entity and time that carries two different
   values *inside* the training data cannot be point-in-time correct either way: it is left out
   of the offline store and counted as `pit_ambiguous` in the report instead. The report's `pit`
   field always says whether the check ran, and why not when it did not;
3. tags the MLflow run with `feature_view`, `feature_view_fingerprint` and
   `feature_view_validated` (`passed`, `failed` under `warn`, or `skipped: <reason>`). The
   fingerprint records which definition the run was bound to; only the third tag says whether its
   data was actually checked against it.

| Variable | Default | Meaning |
|---|---|---|
| `EXAMLOPS_FEATURE_GATE` | `enforce` | `enforce` fails the run closed, `warn` records and continues, `off` skips |
| `EXAMLOPS_FEATURE_GATE_MAX_ROWS` | `1000` | Rows validated per table |
| `EXAMLOPS_FEATURE_GATE_INGEST` | `1` | `0` validates without writing the offline store |

What it cannot check is reported as a skip with a reason, never as a pass: no binding, a `--dummy`
run, or data whose location is not readable here.

## Serving uses the same transform

`FeatureTransformer` transforms every inference request with the pack's serving view (the one
marked `serving: true`, or `EXAMLOPS_SERVING_FEATURE_VIEW`), through the same `transform_row` the
training gate uses. A request whose embedding has the wrong dimension is rejected with the message
training would give the same row. The definition is re-read every `EXAMLOPS_SERVING_FEATURE_TTL`
seconds (60), so an edited file is picked up without a restart.

A request that omits its features but names its entity is served the **materialized online
value** (`EXAMLOPS_SERVING_ONLINE_FEATURES`, on by default):

```json
{"model_name": "JPCP", "job_id": "job-42", "num_nodes": 4}
```

The ingress's own presence check takes its field list from the same serving view, so it admits
this request instead of refusing it for a missing `embedding`; with no resolvable view it keeps
requiring `embedding` and `num_nodes`. Values the request does carry win over stored ones. The lookup happens only on that path; a
request that carries its features costs no store read. If no definition resolves (no pack mounted,
a malformed file), serving falls back to its previous built-in transform and logs why, rather than
failing inference.

## Scheduled materialization

A view with `materialize_interval_seconds` > 0 (in its definition, or `exa feature apply
--interval`) is materialized on that schedule by the control plane, which checks every
`CONTROL_PLANE_FEATURE_MATERIALIZE_SECONDS` (300; `0` disables). Every replica may check; a
per-view coordinator lock makes one of them do the work. At most
`EXAMLOPS_FEATURE_MATERIALIZE_MAX_VIEWS` (50) views are materialized per cycle, and one failing
view does not stop the others. Every run is audited as `feature_view_materialized`, and so is a
manual `exa feature materialize`.

```bash
exa feature status                  # age, TTL, interval, stale/due, online tier, serving view
exa feature materialize-due --dry-run
exa feature materialize-due         # what the control plane does on its tick
```

The control plane's `/metrics` publishes `examlops_feature_view_age_seconds{view}`,
`examlops_feature_view_stale{view}`, `examlops_feature_view_ttl_seconds{view}` and
`examlops_feature_materializations_total{outcome}` (`materialized`, `failed`, `skipped`,
`mirror_failed`). The `FeatureViewStale` alert fires after a view
has been stale for 30 minutes; see the [runbook](../runbooks/control-plane.md#featureviewstale).
A view with neither a TTL nor a schedule is never reported stale.

## Redis online store

```bash
pip install 'examlops[features-online]'
export EXAMLOPS_FEATURE_ONLINE_STORE=redis
export EXAMLOPS_FEATURE_REDIS_URL=redis://redis:6379/0   # falls back to EXAMLOPS_REDIS_URL
```

Materialization always writes the durable `online_features` table first, then mirrors the rows to
Redis (keys under `EXAMLOPS_FEATURE_REDIS_PREFIX`, default `examlops:features`, per view and
entity, expiring after the view TTL). Reads try Redis first and fall back to the table on a miss or
any Redis error (`EXAMLOPS_FEATURE_REDIS_TIMEOUT`, 0.5 s). A failed mirror is reported by
`exa feature materialize`, counted as `examlops_feature_materializations_total{outcome="mirror_failed"}`
by the scheduler, and never undoes the durable write. The keys it did not update are deleted, so
reads fall through to the table rather than serving the value the table has replaced. One case is
left: if Redis refuses both the write and the delete but answers reads later, it serves the previous
value until the next successful mirror or the key's expiry. Without the package or a URL the store degrades to the table, and `exa feature status` says
why. The key expiry bounds Redis memory; it does not stop a stale value being served, because an
expired key is a miss and a miss reads the table. Staleness is reported (`FeatureViewStale`), not
enforced on reads.

## Graceful degradation

No Feast, no Redis and no MinIO are required: the offline log, the online table, materialization
tracking, point-in-time retrieval and skew/freshness checks are pure Python over `platform.db`.

## Related

- **A1** dataset revisions — a view (and its training sets) pin to a revision.
- **A2** lineage — feature views appear as lineage nodes.
- **B5** vector store — the `embedding` feature shares its definition for RAG.
- **C5** data quality & drift — materialization freshness feeds quality monitoring.
