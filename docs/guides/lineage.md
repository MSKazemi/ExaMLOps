# Lineage & provenance graph (OpenLineage)

ExaMLOps emits **OpenLineage** run events so the `data → pipeline → run → model →
deployment` graph is queryable, while `platform_db` stays the operational source of truth.
Emission **dual-writes** both, so the graph and the DB cannot diverge.

**What emits today:** the **training** flow (`START`, dataset → model once the version is
registered, then `COMPLETE` — or `FAIL` with the reason, or `ABORT` when cancelled — see
[one run per training run](#one-run-per-training-run)), a **cost recording** (a child run of the
training run, from `exa models cost --record`), **promotion** (`COMPLETE`, model →
deployment), a **retrain request** (`COMPLETE` once the flow run is scheduled, linked to it), a
**prompt label move** (`COMPLETE`, prompt version → label), plus asset materialization,
fine-tuning, distributed training and `exa data synth`. **Serving emits nothing** —
per-inference lineage is not implemented, so the graph ends at the deployment node.

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
[what the event translates](#what-the-event-translates). Lineage and OTel traces (C1) are correlated,
not merged: an event emitted while a span is active carries its trace id (the
`examlops.trace` facet and the `trace_id` column) with no caller involved, so with tracing on
every path is correlated, and with it off (`OTEL_SDK_DISABLED=true`, the default) none is.

## Querying

```bash
# Upstream (datasets/runs) + downstream (deployments) for a model
exa models lineage jpcp --graph

# Impact analysis — every model version derived from a dataset revision
exa models lineage --impact abc123

exa --json models lineage jpcp --graph        # machine-readable graph
```

The graph lists **runs, not events**. A run that emitted `START` and then `COMPLETE` is one
entry: its newest event, an `events` list with the whole history (`["START", "COMPLETE"]`), and
the newest value of each fact any of its events recorded — so a `FAIL` after a `COMPLETE` does not
hide the dataset revision the run trained on.

There is **no dashboard Lineage page** — ADR 0004's spec anticipates one and it was never
built. `exa models lineage` is the surface.

## Nodes

Nodes are stably namespaced: `examlops://dataset/PM100Dataset@<rev>`,
`examlops://model/jpcp/18`, `examlops://deployment/<name>`. Promotion links the model version
to the deployment it now backs, and records the alias it came from plus the metric that
justified the move. A retrain request records the dataset it asked for and the training run it
scheduled (the `examlops.scheduled_run` facet); the version it will produce is that run's
output, not the request's.

### Facets

Events carry `dataset_revision` (A1), `mlflow_run_id`, `eval_score` (C2, on promotion), cost and
carbon (on the [cost child run](#one-run-per-training-run)), an OTel `trace_id`, a failed run's
`errorMessage` (the standard OpenLineage facet: the reason,
at most 500 characters, never a stack trace), and **`examlops.hpc_job`** — the scheduler job that produced a training run,
carried scheduler-neutrally so the same field serves Slurm, Flux and mock. Only the training
flow sets it: it is the one path that knows a job id at all.

## One run per training run

OpenLineage asks every run for exactly **one `START`** and **one ending** (`COMPLETE`, `FAIL` or
`ABORT`), with anything learned in between sent as `OTHER`. A training run follows that, from start
to finish, under one run id: the **Prefect flow run id** — the id Prefect shows, and the id the
lineage receiver shows.

| Event | Emitted by | When |
|---|---|---|
| `START` | the flow's `on_running` hook | the flow run starts |
| `OTHER` | the flow, after registering the version | what registration learned: dataset → model, the MLflow run id, the dataset revision, the HPC job |
| `COMPLETE` | the flow's `on_completion` hook | the flow run finished |
| `FAIL` | the flow's `on_failure` and `on_crashed` hooks | the flow raised, or its infrastructure died under it — with the reason |
| `ABORT` | the flow's `on_cancellation` hook | the run was cancelled |

The ending comes from Prefect's flow-state hooks, because only the flow's state knows which ending
happened: a run that registered its version and then failed at promotion produced that version
(its `OTHER` says so) and ended in `FAIL`. Hooks also see the endings no `except` inside the
process can catch — a crash, a cancellation. A hook never raises: a lineage outage is logged and
the run's own state is untouched.

A training function called outside a flow run (a direct call, a test) has no flow run id and no
hooks; its registration event is then the whole run — a `COMPLETE` under the MLflow run id.

**One-shot runs** — a promotion, a prompt label move, a retrain request, a cost recording — have
nothing to report until they are over. Their first event is their ending, so the receiver is sent a
`START` just ahead of it. That `START` is not recorded in `platform_db`, which keeps what happened.

**A retrain is a request and a run.** `exa retrain` emits one `COMPLETE` on job
`retrain:<MODEL>` — the request is done the moment its flow run is scheduled — with an
`examlops.scheduled_run` facet naming that flow run and its lineage run id. Following the link
lands on the `train:<model>` run. Before this, the request opened a `START` under the flow run id
that nothing closed, while the flow completed a different run on a different job, so a receiver
showed every retrain as running forever.

**Cost is a child run.** Cost and carbon are known only once the scheduler has accounted the job —
when `exa models cost --record` runs, long after training. Each recording is a `COMPLETE` run of job
`cost:<model>` carrying the `examlops.cost` facet (GPU-hours, cost, kWh, kg CO₂e from the same
carbon provider `exa finops carbon` uses) and the standard `parent` facet naming the training run;
Marquez shows it nested under the training job (`train:<model>.cost:<model>`). The spec's
alternative — an `OTHER` on the finished training run — is what it suggests for late metadata,
and Marquez 0.51.1 answers it by showing the run as running again.

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

