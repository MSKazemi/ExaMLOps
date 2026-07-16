# Declarative Asset-Centric Pipelines (A4)

> Next-Gen 40 · feature **A4** · ADR 0036 · spec `design/vision/specs/A4-asset-centric-pipelines.md`

A4 adds an **asset-centric** layer over the existing task-centric Prefect orchestration.
Instead of thinking about *runs*, you declare the **things** the platform produces —
datasets (A1), features (A3), and models — as **assets** with upstream dependencies. The
platform then:

- builds the **asset DAG** (which coincides with the A2 lineage graph),
- tracks each asset's materialized version against **the upstream versions it was built
  from**, so it knows when an asset is **stale**, and
- rebuilds **only** a target asset plus its stale ancestors — selective, incremental
  materialization, not a full pipeline re-run.

The engine sits behind an `AssetOrchestrator` seam so it is swappable (a thin layer over
Prefect by default, or Dagster). The existing `exa pipeline run` path is **unchanged**.
Everything is pure Python and testable in-process — no Prefect or Dagster required to
declare assets, compute freshness, or materialize.

## Declaring assets

In code with the decorator:

```python
from examlops.assets import asset

@asset("jpcp_features", kind="feature", deps=["PM100"])
def build_jpcp_features(**upstream):
    ...  # upstream = {"PM100": <version>}

@asset("jpcp_model", kind="model", deps=["jpcp_features"])
def train_jpcp(**upstream):
    ...
```

Or from the CLI (source/dataset assets typically have no production function):

```bash
exa assets declare PM100 --kind dataset
exa assets declare jpcp_features --kind feature --deps PM100
exa assets declare jpcp_model --kind model --deps jpcp_features
exa assets graph
# PM100 (source)
# jpcp_features ← PM100
# jpcp_model ← jpcp_features
```

## Freshness

An asset is **stale** when it was never materialized, when any declared upstream advanced
past the version this asset recorded at build time, or when any upstream is itself stale
(transitive):

```bash
exa assets status
# Asset          Version  State   Why
# PM100          1        fresh   —
# jpcp_features  1        fresh   —
# jpcp_model     1        fresh   —
```

## Selective materialization

`materialize` rebuilds the target and its stale ancestors only, dependencies-first:

```bash
exa assets materialize jpcp_model
# Materialized jpcp_model: rebuilt PM100, jpcp_features, jpcp_model   (first run)

exa assets materialize jpcp_model
# jpcp_model already fresh — nothing to rebuild.                       (all fresh)
```

When an upstream dataset revision lands (A1), mark the source changed; only the affected
downstream assets rebuild:

```bash
exa assets source-changed PM100
# PM100 advanced to v2 — downstream assets are now stale.

exa assets materialize jpcp_model
# Materialized jpcp_model: rebuilt jpcp_features, jpcp_model
#   skipped (fresh): PM100
```

This is the core win: a data change re-runs the *stale slice*, not the whole pipeline
(R4 / GWT-3).

## Lineage, policy, audit

Every materialization:

- **emits OpenLineage** (A2) so the asset DAG coincides with the lineage graph (R6). With
  `EXAMLOPS_OPENLINEAGE_URL` set, events also push to Marquez; otherwise they land in
  `platform.db` (fail-open).
- **is policy-governed** (D5): a `deny` on the `asset_materialize` action blocks the run and
  records an `asset_materialize_denied` audit event. No policy file → default allow.
- **is audited** (D4): an `asset_materialize` event records what rebuilt and what was
  skipped.

## Backward compatibility

`exa pipeline run --model JPCP --dataset PM100Dataset` and every existing Prefect flow keep
working exactly as before (R2 / GWT-5). Assets are an additive layer you opt into.

## Related

- **A1** dataset revisions — a source-changed event models a new revision landing.
- **A2** OpenLineage — the asset DAG is the lineage graph.
- **A3** feature store — feature views are feature assets.
- **D4** audit / **D5** policy — every materialization is governed and audited.
