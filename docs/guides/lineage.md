# Lineage & provenance graph (OpenLineage)

ExaMLOps emits **OpenLineage** run events so the `data → pipeline → run → model →
deployment` graph is queryable, while `platform_db` stays the operational source of truth.
Emission **dual-writes** both, so the graph and the DB cannot diverge.

**What emits today:** the **training** flow (`COMPLETE`, dataset → model), **promotion**
(`COMPLETE`, model → deployment), **retrain** (`START` — the retrain is scheduled, and the
flow emits its own completion), a **prompt label move** (`COMPLETE`, prompt version → label),
plus asset materialization, fine-tuning, distributed training and `exa data synth`. **Serving
emits nothing** — per-inference lineage is not implemented, so the graph ends at the deployment
node.

That boundary is what decides *where* an event belongs. A prompt version is an input to every
gateway call that resolves it, so emitting there would mean one event per inference; the **label
move** is the release, and that is what the graph records.

**Marquez is not shipped.** ADR 0004 names it as the reference receiver under a docker-compose
`lineage` profile; no such profile exists. Set `EXAMLOPS_OPENLINEAGE_URL` to any OpenLineage
receiver you run yourself, or read the graph from `platform_db` with `exa models lineage`,
which is what the CLI does today.

Design: ADR 0004 · spec `design/vision/specs/A2-open-lineage.md`.

## Fail-open by design

`emit_lineage()` never fails a pipeline (R6):

- `EXAMLOPS_OPENLINEAGE_URL` **unset** → no HTTP is sent; the `platform_db` rows are still
  written (the graph degrades to the operational DB).
- URL **set but unreachable** → the failure is logged and swallowed.

## Emitting

```python
from examlops import lineage

lineage.emit_lineage(
    "COMPLETE",
    job="train.jpcp",
    run_id=run_id,
    inputs=[lineage.dataset_node("PM100Dataset", revision)],   # A1 revision
    outputs=[lineage.model_node("jpcp", version)],
    dataset_revision=revision,
    mlflow_run_id=mlflow_run_id,
    model="jpcp",
    model_version=version,
    facets={**lineage.cost_facet(gpu_hours=12.5), **lineage.eval_facet(0.92, "rmse")},
)
```

Custom facets carry a `_producer` + `_schemaURL` and use the `examlops.` prefix (R12);
events validate against the OpenLineage run-event schema (R11). Correlate lineage with
OTel traces (C1) via the `trace_id` facet.

## Querying

```bash
# Upstream (datasets/runs) + downstream (deployments) for a model
exa models lineage jpcp --graph

# Impact analysis — every model version derived from a dataset revision
exa models lineage --impact abc123

exa --json models lineage jpcp --graph        # machine-readable graph
```

There is **no dashboard Lineage page** — ADR 0004's spec anticipates one and it was never
built. `exa models lineage` is the surface.

## Nodes

Nodes are stably namespaced: `examlops://dataset/PM100Dataset@<rev>`,
`examlops://model/jpcp/18`, `examlops://deployment/<name>`. Promotion links the model version
to the deployment it now backs, and records the alias it came from plus the metric that
justified the move; a retrain links the dataset to the model it will produce.

### Facets

Events carry `dataset_revision` (A1), `mlflow_run_id`, `cost`/`carbon`, `eval_score` (C2), an
OTel `trace_id`, and **`examlops.hpc_job`** — the scheduler job that produced a training run,
carried scheduler-neutrally so the same field serves Slurm, Flux and mock. Only the training
flow sets it: it is the one path that knows a job id at all.
