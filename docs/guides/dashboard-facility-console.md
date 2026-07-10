# Facility Console

The **Facility Console** is the dashboard's HPC operations view. It renders the platform's
scheduler abstraction (phase 23 — mock / Slurm / Flux) as a scheduler-neutral overview: how many
nodes and GPUs are allocated, how deep the queue is, per-partition pressure, and the list of waiting
jobs. It is read-only and degrades gracefully when telemetry is missing.

Open it from the sidebar (**Facility**, the `Cpu` icon) or navigate to `/facility`.

- **Feature:** F6 · **Design:** [ADR 0059](../../design/adr/0059-dashboard-exascale-facility-console.md) ·
  **Spec:** `design/vision/specs/F6-exascale-facility-console.md`
- **Backend:** `platform/services/dashboard/backend/facility.py` + `routers/facility.py`
- **Frontend:** `platform/services/dashboard/frontend/src/lib/facility.ts` + `pages/FacilityConsole.tsx`

## What you see

### KPI cards

| KPI | Meaning |
|---|---|
| **Nodes allocated** | Sum of node asks across jobs in a running state. |
| **GPUs allocated** | Sum of GPU asks across running jobs. |
| **Jobs running** | Count of jobs currently executing. |
| **Queue depth** | Count of jobs waiting (`SUBMITTED` / `PENDING`). |

### Partitions

One row per scheduler/cluster, showing running vs queued counts and allocated GPUs. A colour-blind-safe
`StatusPill` flags **Backlog** when the queue exceeds running work (`partitionTone`), so a saturated
partition is obvious at a glance without relying on colour alone.

### Cluster switcher

When more than one scheduler/cluster has recorded jobs, a **Cluster** dropdown appears. Selecting a
cluster rescopes every KPI, partition, and queue row to that cluster (the same UI works over mock,
Slurm, and Flux — F6 R6). "All" aggregates across clusters.

### Queue

Waiting jobs, **longest-wait first**. `hpc_jobs` has no fair-share/priority column, so wait time
(`queue_seconds`) is the ordering signal. Each row shows the job id, cluster, model, GPU ask, and a
human wait label (`30s` / `2m` / `1h 5m`).

## How it maps to the CLI / platform

The console reads the `hpc_jobs` table that the HPC scheduler abstraction writes (phase 23):

```bash
# hpc_jobs rows are produced by pipeline runs on the mock/Slurm/Flux adapters:
EXAMLOPS_HPC_SCHEDULER=slurm exa pipeline run --model JPCP --dataset PM100Dataset
exa models cost JPCP --record     # links a job's GPU-hours to MLflow (the mlflowRunId cost link)
```

Per-job detail (`GET /api/v1/facility/job/{id}`) exposes the `mlflowRunId` cost link that ties an HPC
job back to its MLflow run and `model_costs` accounting.

## Endpoints

All require the `viewer` role, are composed through the F8 BFF substrate, and accept an optional
`?cluster=<scheduler>` filter:

| Endpoint | Returns |
|---|---|
| `GET /api/v1/facility/overview` | `{facility: {nodesAllocated, gpusAllocated, jobsRunning, queueDepth, clusters, partitions}}` |
| `GET /api/v1/facility/queue` | `{queue: {jobs, count}}` |
| `GET /api/v1/facility/job/{id}` | `{job: {resources, timing, mlflowRunId, …}}` (`404` if unknown) |

See [`docs/reference/api.md`](../reference/api.md) for full shapes and
[`docs/dashboard/architecture.md`](../dashboard/architecture.md#facility-console-f6) for the component
diagram.

## Notes & limits

- Reads from `platform.db` (`PLATFORM_DB`); a missing `hpc_jobs` table or empty DB renders zeros /
  "no data", never an error (F6 R7).
- This slice covers overview + partitions + queue + job detail. The richer F6 surfaces from the spec
  (utilization heatmap, node/GPU inspector with MIG/MPS, `@xyflow` topology, storage, reservations,
  power/thermal/carbon) build on this substrate and are tracked in the dashboard-nextgen plan.
