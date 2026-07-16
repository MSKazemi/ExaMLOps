# Project Anatomy — Storage, Pipelines & the Unified View

This guide covers the **Project Anatomy** layer (Increment 2 of Projects & Workspaces): the
per-project **MinIO storage location**, the two **pipeline surfaces** (Prefect training + Ray Serve
serving), and the **assembled anatomy view** that renders storage + connections + pipelines + models +
members + quota + budget as one governable unit — in both `exa project` and the dashboard.

It builds on [Projects & Workspaces](projects-workspaces.md) (the project primitive, Named
Connections, membership, and cost). Read that first if a project is new to you.

> **Mental model.** The anatomy is a *view over records*, not a running system. A project's storage
> is a **convention + a small binding record**; its pipelines are an **aggregation over existing
> Prefect deployments and Ray-served models**. Nothing here launches containers on its own — the
> value is making a team's whole ML footprint **named, visible, and consistent** across the CLI and
> dashboard.

---

## 1. Per-project storage (P6)

Every project gets a stable MinIO location, so a team's pipeline artifacts, datasets, and cache no
longer share one global bucket:

```
s3://examlops-projects/<project>/
    ├── artifacts/   # MLflow run artifacts (bound via a per-project MLflow experiment)
    ├── datasets/    # dataset snapshots for the project
    └── cache/       # scratch / intermediate
```

- The bucket defaults to `examlops-projects` (override with `EXAMLOPS_PROJECTS_BUCKET`); the compose
  `minio-init` service creates it on startup.
- The prefix is always `<project>/`, so the split is **shared bucket, per-project prefix** — no new
  IAM or bucket provisioning per project.
- `projects.storage_gb` stays the **quota**; a small additive `project_storage` table records the
  binding and observed `used_bytes`.
- Training routes into the project prefix through a **per-project MLflow experiment**
  (`project/<name>`) whose `artifact_location` points at `.../artifacts/`. When a model belongs to a
  project, `exa pipeline run` and the pipeline generator set that experiment automatically (fail-open:
  if project resolution or MinIO is unavailable, training falls back to the default experiment).

### Show, bind, refresh

```bash
exa project storage climate-team                     # show the location, used GB, and any bound connection
exa project storage climate-team --refresh           # re-probe used bytes from MinIO (fail-open)
exa project storage climate-team --bind-connection raw-s3   # point storage at a P2 S3 connection
```

Binding a [Named Connection](projects-workspaces.md#connections) sets the project's bucket from that
connection's config and records `connection_ref` — the **credentials stay in D7 secrets and are never
copied** into `project_storage`. Use this when a project should live in a dedicated bucket rather than
the shared `examlops-projects` one.

`exa project storage <name> --json` (or the global `-o json|yaml|csv`) emits the raw record.

---

## 2. The two pipeline surfaces (P7)

A project exposes exactly **two** pipeline surfaces, each an aggregation over state that already
exists — no heavyweight per-project pipeline objects are created:

| Surface | What it is | Derived from |
|---|---|---|
| **Prefect (training)** | The project's training deployments, schedule, last run, status | Deployments tagged `project:<name>` (from `deploy.py`), plus the optional `project_pipelines` registry row |
| **Ray Serve (serving)** | The project's served models and their traffic split | Models where `resolve_project(model) == <name>`, joined with `traffic_rules` |

```bash
exa project pipelines climate-team
```

```
Prefect pipeline (training)
  Deployments   examlops-jpcp
  Schedule      0 2 * * *
  Last run      2026-07-15 02:00
  Status        healthy

Ray Serve pipeline (serving)
  Model   Traffic split
  JPCP    {'Production': 90, 'Canary': 10}
```

An empty project (no models assigned yet) shows both surfaces as absent — assign a model first:

```bash
exa project assign climate-team JPCP --kind model
```

The optional `project_pipelines` table is a light registry/cache keyed by `(project, kind)` where
`kind ∈ prefect | rayserve` — so a project holds **one Prefect surface and one Ray Serve surface**, by
primary key. The training surface writes to the project's P6 storage; the serving surface serves the
project's promoted models.

---

## 3. The assembled anatomy view (P8)

`exa project show <name>` renders the whole workspace in one pane — storage, connections, pipelines,
models, members, quota, and budget — reading the **same** `get_project_full()` model the dashboard
uses, so the CLI and dashboard say the exact same words.

```bash
exa project show climate-team
```

The view adds three panels on top of the Increment-1 anatomy:

- **Storage** — location, used GB against quota, bound connection (P6).
- **Connections** — each project connection by name and kind, with a `has_secret` flag only — the
  **secret value and its `secret_ref` are never transported** (P2).
- **Pipelines** — the Prefect and Ray Serve surfaces from §2 (P7).

Every source is **fail-open**: if MinIO, Prefect, Ray, or the connections layer is unavailable, the
corresponding panel degrades to empty rather than failing the whole view.

### In the dashboard

The **Projects** console → project detail page shows the same anatomy: a **Storage** card with a usage
bar, a **Pipelines** card with the Prefect and Ray Serve surfaces, alongside the existing Models,
Members, Quota, and Budget cards. `GET /api/v1/projects/<name>` returns the extended anatomy
(`storage`, `connections`, `pipelines`), viewer-gated and secret-safe.

---

## 4. Isolation model

A project is a **governable boundary**, honest about enforcement depth on the Docker/MinIO/SQLite
substrate:

| Boundary | Mechanism | Enforcement |
|---|---|---|
| Network | `network_name = examlops-<name>` (per-project docker bridge) | advisory (compose) |
| Storage | per-project MinIO prefix (P6) | soft (shared bucket); hard via `--bind-connection` → dedicated bucket |
| Connections | `connections` PK `(project, name)`; credentials in D7 secrets | scoped, secret-safe |
| People / RBAC | `authz_relations` owner ⊇ editor ⊇ viewer on `project:<name>` | checked (default-deny under `EXAMLOPS_MULTITENANCY`) |
| Compute quota | project CPU/mem/GPU/storage limits; `exa project compose` | advisory (compose) |
| Cost | `model_costs.project` | attributed |
| Audit | `audit_events` with the project as target | recorded |

Hard enforcement (kernel-level network firewalling, per-bucket IAM, cluster-level queue admission) is
a future live-runtime increment; the anatomy layer delivers the **named, visible, consistent**
boundary.

---

## 5. Command summary

| Command | Purpose |
|---|---|
| `exa project show <name>` | Full anatomy: storage · connections · pipelines · models · members · quota · budget |
| `exa project storage <name>` | Show the MinIO storage location + usage |
| `exa project storage <name> --bind-connection <conn>` | Point storage at a P2 S3 connection |
| `exa project storage <name> --refresh` | Re-probe used bytes from MinIO |
| `exa project pipelines <name>` | The Prefect (training) + Ray Serve (serving) surfaces |
| `exa pipeline run --project <name>` | Train a project model into its storage prefix |

## 6. Related

- [Projects & Workspaces](projects-workspaces.md) — the project primitive, connections, membership, cost
- [Architecture — The ExaMLOps Project Workspace](../architecture/project-workspace.md) — C4 diagrams, data model, sequence
- `exa connection --help` — creating and scoping Named Connections
