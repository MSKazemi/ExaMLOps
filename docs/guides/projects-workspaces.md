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

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_PROJECT` | unset | Active project override (wins over `config.toml`). Set by `exa project use`. |
| `EXAMLOPS_MULTITENANCY` | unset (off) | Enables RBAC enforcement of project membership. |
| `EXAMLOPS_ACTOR` | `$USER` | Actor recorded in audit events for project mutations. |
| `PLATFORM_DB` | `./platform.db` | Shared SQLite DB holding `projects`, `project_resources`, `project_models`, `project_budgets`. |

## Related

- ADR 0086 (unified workspace), 0084 (Docker resource envelopes), 0057 (authn/authz/multitenancy)
- Spec `design/vision/specs/P1-unified-project-workspace.md`
- SoA dossier `design/vision/library/rhoai-soa-projects.md`
- Roadmap: `docs/guides/*` for P2 Named Connections, P3 project-scoped serving/pipelines, P4 project
  FinOps, P5 workbenches (written as each ships) · plan `.claude/plans/projects-workspace/`
- `docs/reference/commands.md`, `docs/guides/rbac-multi-tenancy.md`
