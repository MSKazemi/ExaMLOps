# Projects & Workspaces

A **Project** is ExaMLOps's canonical *workspace*: one named unit that groups a team's models,
pipelines, serving endpoints, connections, and datasets, together with the people who can touch them
(owner/editor/viewer) and the resource quota and cost attributed to them. The concept is inspired by
Red Hat OpenShift AI *Data Science Projects*, and unifies four grouping primitives that used to be
separate (projects, namespaces, authz relations, and the tenant claim) behind a single key.

> Design principle: a project is the one place where a team's ML footprint is **isolated,
> attributable, and governable as a unit** — see ADR 0086 and spec
> `design/vision/specs/P1-unified-project-workspace.md`.

## 1. Create and inspect a project

```bash
exa project create research --cpu-limit 4 --memory-gb 8 --storage-gb 100
exa project list
exa project show research          # full anatomy: quota · resources · members · budget · consumption
exa --json project show research   # machine-readable (also -o yaml|csv)
```

`show` renders everything the project owns: its resources grouped by kind, its members and their
roles, the resource quota, any FinOps budget, and the GPU-hour/cost consumption attributed to it.

## 2. Assign resources

A project can group any resource kind — `model`, `pipeline`, `serving_endpoint`, `connection`,
`dataset`, `storage`:

```bash
exa project assign research JPCP --kind model
exa project assign research jpcp-train --kind pipeline
exa project assign-model research MACK    # legacy alias for --kind model
```

Model assignments are dual-written to the legacy `project_models` table and the unified
`project_resources` table, so existing tooling keeps working during the transition.

## 3. Members & permissions

People membership uses the platform's relationship RBAC (D6): a member is a subject with an
`owner` ⊇ `editor` ⊇ `viewer` relation on `project:<name>` — mapping to RHOAI Admin/Edit/View. No
separate ACL store is introduced.

```bash
exa project add-member research alice --role editor
exa project add-member research bob --role viewer
exa project members research
exa project remove-member research bob        # all roles, or --role viewer for one
```

Grants are enforced only when `EXAMLOPS_MULTITENANCY` is truthy (single-tenant passthrough
otherwise). A grant on `project:<name>` cascades to its child objects (e.g. `project:<name>/model:X`).

## 4. Active project context

Set an active project so other tools can scope to it (mirrors `exa config use`):

```bash
exa project use research
exa project current
```

Resolution precedence: the `EXAMLOPS_PROJECT` environment variable wins, then `active_project` in
`config.toml`, then none.

## 5. Quota & Docker Compose (from ADR 0084)

```bash
exa project set-quota research --cpu-limit 8 --memory-gb 16
exa project compose research --out docker-compose.project.yml
```

`compose` emits a quota-bounded Docker Compose fragment (per-service `deploy.resources.limits`, a
per-project bridge network, named volumes, and NVIDIA reservations when the project has a GPU quota).

## 6. Cost attribution

When a model is a member of a project, `exa models cost --record` tags the recorded cost with that
project (the new `model_costs.project` column). `exa project show` then reports the project's
consumption. Historical costs recorded via the legacy namespace grouping are still counted through a
union read path, so no attribution is lost.

## 7. Dashboard

The **Projects console** (gated by the `projectsConsole` feature flag) lists projects and shows each
project's anatomy (resources, members, quota, budget, consumption). Viewers can browse; the
create/assign/add-member actions require the `project.manage` capability (admin). Every mutation is
audited and visible under `exa audit` and the dashboard Audit page.

## 8. Named Connections (P2, ADR 0087)

Reusable, project-scoped data connections (S3 / URI / dataplane). Non-secret config is stored in
`platform.db`; credentials live only in the D7 secrets client (referenced by `secret_ref`, never
copied). A connection is also a project resource.

```bash
exa connection create minio --kind s3 --project research \
    --config '{"endpoint":"http://localhost:19000","bucket":"data","access_key":"minioadmin"}' \
    --secret-value minioadmin
exa connection list --project research
exa connection show minio --project research      # never prints the secret value
exa connection test minio --project research      # read-only reachability probe (exit 1 if unreachable)
exa connection delete minio --project research
```

## 9. Project-scoped serving & pipelines (P3, ADR 0088)

A model's owning project is threaded into serving and pipelines. `GET /models` includes each model's
`project`; Prefect deployments are tagged `project:<name>`; a model YAML may declare a default
`project:`; and a scoped run attributes its cost:

```bash
exa pipeline run --model JPCP --project research   # tags the run + attributes recorded cost
```

## 10. Project FinOps (P4, ADR 0089)

```bash
exa project cost research      # attributed GPU-hours · USD · carbon (model_costs.project)
exa project budget research    # budget/quota status; exits 1 and audits a breach if over budget
```

## 11. Workbenches (P5, ADR 0090)

On-demand, project-bound dev environments. Starting one injects the project's Named Connections (P2)
as `EXA_CONN_<name>_<key>` / `EXA_CONN_<name>_SECRET` env vars; the actual spawn is delegated to the
runtime (JupyterHub/Docker).

```bash
exa workbench create nb --project research --image jupyter/scipy-notebook:latest
exa workbench start nb --project research     # marks RUNNING, prints launch spec + injected env count
exa workbench list --project research
exa workbench stop nb --project research
exa workbench delete nb --project research
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_PROJECT` | unset | Active project override (wins over `config.toml`). Set by `exa project use`. |
| `EXAMLOPS_MULTITENANCY` | unset (off) | Enables RBAC enforcement of project membership. |
| `EXAMLOPS_ACTOR` | `$USER` | Actor recorded in audit events for project mutations. |
| `PLATFORM_DB` | `./platform.db` | Shared SQLite DB holding `projects`, `project_resources`, `project_models`, `project_budgets`. |

## Related

- ADRs 0086 (unified workspace), 0087–0090 (P2–P5, all Accepted), 0084 (Docker resource envelopes),
  0057 (authn/authz/multitenancy)
- Specs `design/vision/specs/P1`–`P5-*.md`
- SoA dossier `design/vision/library/rhoai-soa-projects.md` · plan `.claude/plans/projects-workspace/`
- `docs/reference/commands.md`, `docs/guides/rbac-multi-tenancy.md`
