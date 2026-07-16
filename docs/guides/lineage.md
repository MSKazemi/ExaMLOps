# Lineage & provenance graph (OpenLineage)

ExaMLOps emits **OpenLineage** run events from training, promotion, retrain, and
(optionally, sampled) serving so the full `data → pipeline → run → model → deployment →
inference` graph is queryable in **Marquez** — while `platform_db` stays the operational
source of truth. Emission **dual-writes** both, so the graph and the DB cannot diverge.

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

The dashboard **Lineage** page renders the graph read-only from the Marquez API when
`EXAMLOPS_OPENLINEAGE_URL` is configured; otherwise it renders from `platform_db`.

## Nodes

Nodes are stably namespaced: `examlops://dataset/PM100Dataset@<rev>`,
`examlops://model/jpcp/18`, `examlops://deployment/<name>`. Promotion/alias changes and
drift-triggered retrains emit events linking the affected versions (R4).
