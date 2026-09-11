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

**A receiver ships as an opt-in profile.** Marquez, the OpenLineage reference server, runs under
the Compose `lineage` profile ([below](#running-marquez-the-lineage-profile)). Any other
OpenLineage receiver works the same way — point `EXAMLOPS_OPENLINEAGE_URL` at it. Without one,
`exa models lineage` reads the same graph from `platform_db`, which is always written.

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
events validate against the OpenLineage 2-0-2 run-event schema (R11) — see
[what the event translates](#what-the-event-translates). Correlate lineage with OTel traces (C1)
via the `trace_id` facet.

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

## Running Marquez: the `lineage` profile

```bash
cd platform/infra/docker-compose
docker compose --profile lineage up -d marquez-web     # starts marquez-db and marquez too
```

| | Host | From a container |
|---|---|---|
| Marquez API (OpenLineage endpoint) | `http://localhost:15050` | `http://marquez:5000` |
| Marquez web UI | `http://localhost:13050` | — |

Then tell the emitters where it is. They run in two places, and each needs the address it can
reach:

- **Containers** (the dataplane's pulls, the dashboard's CLI console, the control plane): set
  `EXAMLOPS_OPENLINEAGE_URL=http://marquez:5000` in `platform/infra/docker-compose/.env` and
  recreate them. Set it in that file rather than exporting it in your shell: Compose lets a shell
  variable win, and `localhost` inside a container is the container itself.
- **Host processes** (the training flow served by `exa pipeline deploy`, `exa` commands):
  `export EXAMLOPS_OPENLINEAGE_URL=http://localhost:15050`.

Emission stays fail-open either way: a stopped Marquez costs the graph those events, never a run.

!!! warning "Marquez has no authentication"
    Anyone who can reach its API can write lineage or delete it, and the web UI proxies that API.
    The profile therefore publishes both ports on **loopback only**, runs the API and UI as an
    unprivileged user with a read-only filesystem and no Linux capabilities, and keeps Marquez's
    database on an internal network only Marquez joins (in the segmented overlay). Do not
    publish either port beyond the host; put an authenticating proxy in front if people elsewhere
    need the UI.

**What it keeps.** Marquez's database (`marquez_db_data`) is a *view* built from the events it
has received. `platform_db` remains the record — `exa backup` does not include the Marquez volume,
and an event sent while Marquez was down is not replayed into it.

**Upgrading.** The images are pinned by digest (Marquez 0.51.1). A new release is a new digest and
a restart; Marquez migrates its own schema on start (`migrateOnStartup`). Its configuration is
`platform/infra/docker-compose/marquez/marquez.yml` (search off — the stack runs no OpenSearch).

## What the event translates

The platform's own identifiers do not all fit the OpenLineage schema, so the **event** translates
them. The `platform_db` rows keep the platform's identifiers, which is what `exa models lineage`
and `--impact` read.

| | In `platform_db` | In the OpenLineage event | Why |
|---|---|---|---|
| Run id | the platform's id | a UUID (`run.runId`) | the schema requires `format: uuid` |
| Dataset revision | in the node name: `FData@abc123` | name `examlops://dataset/FData`, revision in the standard `version` facet | a revision is a version of one dataset, not a new dataset |
| Free-form values (`{"backend": "minio"}`) | as given | gathered into one `examlops.run` facet | a facet must be an object with `_producer` and `_schemaURL` |

**How a run id becomes a UUID.** An MLflow run id is 32 hexadecimal digits — a UUID without its
dashes — so it is sent as that UUID: the run in Marquez and the run in MLflow share an id. Any
other id (`train-jpcp-FData`, `retrain-jpcp-7`, `prompt-greeting-prod-v2`) becomes a name-based
UUID (RFC 9562 version 5) under a fixed namespace, so it is deterministic: a run's `START` and
`COMPLETE` events land on one run, and re-sending an event cannot create a second one. Whenever the
id changed, the original is kept in the `examlops.run` facet's `run_id`.

**What a receiver does with dataset versions is its own business.** Marquez creates a new dataset
version when a job *writes* a dataset. For a dataset a job *reads*, it keeps the version facet of
the most recent read, so the dataset page shows the newest revision a run consumed. Which revision
each run read stays on that run, in its `examlops.dataset_revision` facet.

`tests/unit/test_lineage_openlineage_conformance.py` validates events against the published
schemas (vendored under `tests/unit/fixtures/openlineage/`), and
`tests/integration/test_lineage_marquez_live.py` sends them to a running Marquez and reads back
what it stored (opt-in: `EXAMLOPS_LINEAGE_LIVE_MARQUEZ_URL=http://localhost:15050`).

