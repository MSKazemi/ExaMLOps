# Projects & Workspaces

A **Project** is ExaMLOps's canonical *workspace*: one named unit that groups a team's
models, pipelines, serving endpoints, connections, and datasets, together with the people
who may touch them (owner / editor / viewer), the resource quota that bounds them, and the
cost attributed to them.

A project is the single place where a team's ML footprint becomes **isolated, attributable,
and governable as a unit**. It replaces four grouping primitives that used to be separate —
projects, namespaces, authorization relations, and the tenant claim — behind one key.

> **Mental model.** A project is a *declaration and a grouping*, not a running system. Creating
> one records metadata and access grants; it does **not** by itself launch containers, allocate
> GPUs, or provision storage. The declared quota becomes real Docker limits only when you
> materialize it with `exa project compose` (see §6). This keeps project creation instant,
> reversible, and free of side effects.

---

## What a project actually is

| You see in the UI / CLI | What it is under the hood | Does it provision anything? |
|---|---|---|
| **Project** (name, status `ACTIVE`) | A row in the `projects` table with declared CPU / memory / storage / GPU limits | No — metadata only |
| **Quota** (e.g. 8 cores · 32 GB · 10 GB · 1 GPU) | Declared limit fields on the project row | No — a *ceiling*, not an allocation. Enforced when composed (§6) |
| **Resources** (dataset / model / pipeline / storage …) | Rows in `project_resources` (`project`, `kind`, `ref`) — **references** to existing assets | No — attaches an asset that already exists |
| **Members** (owner / editor / viewer) | Rows in `authz_relations` on `project:<name>` | No — access grants |
| **Budget & Consumption** | Consumption is **computed** by summing `model_costs` for the project's models | No — reporting only |

Because everything above is a database record, projects are cheap: create, archive, and delete
are metadata operations, every mutation is audited, and nothing is left running when a project
is removed.

---

## What happens in the background when you create a project

Creating a project — via `exa project create` or the dashboard **Assign resource / Create**
flow — performs exactly these steps:

1. **Inserts one row** into the `projects` table: the name, description, the declared quota
   (`cpu_limit`, `memory_limit_gb`, `storage_gb`, `gpu_limit`), a derived `network_name`
   (`examlops-<name>`), `status = ACTIVE`, and the creator.
2. **Grants the creator the `owner` role** by writing an `authz_relations` row
   (`subject = you`, `relation = owner`, `object = project:<name>`). This is the "Members"
   table you see on the project page.
3. **Writes an audit event** (`project_created`) to `audit_events`, visible under
   `exa audit` and the dashboard Audit page.

That is the whole footprint. **No containers, networks, volumes, or GPUs are provisioned.**
The `network_name` and the quota numbers are *declarations* that describe what a compose run
(§6) would create — they are inert until you run it.

---

## 1. Create and inspect a project

```bash
exa project create research --cpu-limit 4 --memory-gb 8 --storage-gb 100
exa project list
exa project show research          # full anatomy: quota · resources · members · budget · consumption
exa --json project show research   # machine-readable (also -o yaml|csv)
```

`show` renders everything the project owns: its resources grouped by kind, its members and
their roles, the resource quota, any FinOps budget, and the GPU-hour / cost consumption
attributed to it.

## 2. Assign resources

A project can group any resource kind — `model`, `pipeline`, `serving_endpoint`,
`connection`, `dataset`, `storage`. Assigning **links an asset that already exists**; it does
not create the underlying model, dataset, or pipeline.

```bash
exa project assign research JPCP --kind model
exa project assign research jpcp-train --kind pipeline
exa project assign-model research MACK    # legacy alias for --kind model
```

Model assignments are dual-written to the legacy `project_models` table and the unified
`project_resources` table, so existing tooling keeps working during the transition.

## 3. Members & permissions

People membership uses the platform's relationship-based access control: a member is a subject
with an `owner` ⊇ `editor` ⊇ `viewer` relation on `project:<name>`. No separate ACL store is
introduced.

```bash
exa project add-member research alice --role editor
exa project add-member research bob --role viewer
exa project members research
exa project remove-member research bob        # all roles, or --role viewer for one
```

Grants are enforced only when `EXAMLOPS_MULTITENANCY` is truthy (single-tenant passthrough
otherwise). A grant on `project:<name>` cascades to its child objects
(e.g. `project:<name>/model:X`).

## 4. Active project context

Set an active project so other tools can scope to it (mirrors `exa config use`):

```bash
exa project use research
exa project current
```

Resolution precedence: the `EXAMLOPS_PROJECT` environment variable wins, then `active_project`
in `config.toml`, then none.

## 5. Quota

The quota is a **declared ceiling** — how much CPU, memory, storage, and GPU the project's
workloads are allowed to consume. It is stored on the project row and reported by `show`; it
becomes an enforced limit only when materialized into a compose fragment (§6).

```bash
exa project set-quota research --cpu-limit 8 --memory-gb 16
```

## 6. Materialize the quota into Docker Compose

This is the step that turns a project's *declaration* into *running infrastructure*:

```bash
exa project compose research --out docker-compose.project.yml
```

`compose` emits a quota-bounded Docker Compose fragment — per-service
`deploy.resources.limits`, a per-project bridge network, named volumes, and NVIDIA GPU
reservations when the project has a GPU quota. You then apply that fragment yourself. Until you
do, the project owns no live resources.

## 7. Cost attribution

When a model is a member of a project, `exa models cost --record` tags the recorded cost with
that project (the `model_costs.project` column). `exa project show` then reports the project's
consumption. Consumption is **computed**, not tracked live — it sums recorded cost rows for the
project's models — so a fresh project reports `0 GPU-hours · $0` until a run records a cost.
Historical costs recorded via the legacy namespace grouping are still counted through a union
read path, so no attribution is lost.

## 8. Dashboard

The **Projects console** (gated by the `projectsConsole` feature flag) lists projects and shows
each project's anatomy (resources, members, quota, budget, consumption). Viewers can browse; the
create / assign / add-member / **remove-member** / **delete-project** actions require the
`project.manage` capability (admin). Every mutation is audited and visible under `exa audit` and the
dashboard Audit page.

Since the *dashboard edit-parity* work, the config-setup operations an operator previously had to run
from the CLI are available in the UI. Each write router calls the **same `examlops.*` code path the
CLI calls** (no raw-sqlite mirror), so the dashboard can never drift from the CLI and secrets are
written through the same secrets client:

- **Connections** — viewers see a read-only list (name, kind, *secret set / none* — the value is
  never rendered). Admins (`connection.manage`) get **New connection** (S3 / URI / dataplane, optional
  encrypted secret), per-row **Test** (reachability probe) and **Delete**. A secret entered here is
  written through `examlops.secrets` (the same store `exa connection create` uses), so it is readable
  by the CLI and serving layer — only `hasSecret` ever returns to the browser.
- **Storage** — admins can **Provision storage** (the per-project MinIO layout) and **Bind** one of
  the project's connections to it (calls the shared `ensure_project_storage` / `bind_project_connection`
  helpers, matching `exa project storage`).
- **Members** — admins add and now **remove** members (owner / editor / viewer via the D6 authz
  relations); each change is audited.
- **Danger zone** — admins can **Delete project**: removes the project grouping plus its membership /
  resource / storage / pipeline rows. The underlying models and connections are *not* deleted.
- **Workbenches** — the project's dev environments (§12) with their status. Admins get a per-row
  Start / Stop toggle (`RUNNING` ↔ `STOPPED`); the flip is audited. The actual pod spawn is
  delegated to the runtime — the dashboard records and reports intent.

Every mutation is capability-gated at the BFF (the sole enforcement point), viewer-denied with a
human `deny_reason`, and audited as a `dashboard`-sourced event.

## 9. Named Connections

Reusable, project-scoped data connections (S3 / URI / dataplane). Non-secret config is stored in
`platform.db`; credentials live only in the secrets client (referenced by `secret_ref`, never
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

A connection's `--kind` is not limited to `s3`/`uri`/`dataplane`: it also accepts every kind a
registered [dataplane](dataplane.md) connector declares (`sql`, `rest`, `kafka`, `zenodo`, …), so
the same Named Connection created here is what `exa dataplane sources create --connection <name>`
binds a pulled dataset to — one credential, reused by both project storage and the dataplane.

## 10. Project-scoped serving & pipelines

A model's owning project is threaded into serving and pipelines. `GET /models` includes each
model's `project`; Prefect deployments are tagged `project:<name>`; a model YAML may declare a
default `project:`; and a scoped run attributes its cost:

```bash
exa pipeline run --model JPCP --project research   # tags the run + attributes recorded cost
```

## 11. Project FinOps

```bash
exa project cost research      # attributed GPU-hours · USD · carbon (model_costs.project)
exa project budget research    # budget/quota status for the budget's period; exits 1 if over
```

**A budget is compared against its period.** A budget row carries one — `monthly` by default —
and the consumption it is checked against is the spend *inside that window* (the current calendar
month, UTC). `total` means lifetime. `exa project budget` shows the period, the window start, the
spend inside it, and the lifetime figure beside it.

!!! warning "Fixed after v0.58.0 — a monthly budget used to be compared against all spend ever"
    Every recorded cost was summed against the budget whatever its period said, so a monthly budget
    breached permanently once lifetime spend passed it and never reset at the month boundary.

**A breach announces itself.** The governance event is written when the state *changes*:

| Event | When |
|---|---|
| `project_budget_breach` | the project enters breach, or an already-breached project breaches something else as well |
| `project_budget_recovered` | it leaves breach — including because an operator raised the budget |

Nothing is written while the state holds, so a breached project does not collect one event per
look. Evaluation happens where the spend becomes known — `exa models cost --record` checks the
project of each model it records — and on demand from `exa project budget`. There is no daemon to
run, and a governance-write failure never fails the recording.

Enforcement stays advisory (ADR 0084): a project over budget is announced and shown, not blocked.

## 12. Workbenches

On-demand, project-bound dev environments. Starting one injects the project's Named Connections
(§9) as `EXA_CONN_<name>_<key>` / `EXA_CONN_<name>_SECRET` env vars; the actual spawn is
delegated to the runtime (JupyterHub / Docker).

```bash
exa workbench create nb --project research --image jupyter/scipy-notebook:latest
exa workbench start nb --project research     # marks RUNNING, prints launch spec + injected env count
exa workbench list --project research
exa workbench stop nb --project research
exa workbench delete nb --project research
```

## 13. Agent surface (MCP)

Projects are callable by LLM agents over MCP (`exa mcp serve`). Read tools `project_list`,
`project_detail`, and `project_cost` let an agent inspect the workspace graph — membership, quota,
budget vs. consumption — with no secret exposure. The write tools `project_assign_model` and
`project_add_member` are registered only when writes are enabled (`--allow-writes` /
`EXAMLOPS_MCP_ALLOW_WRITES=1`), pass the same least-privilege `agent_write` policy gate as every
other mutating tool, and are audited. See `docs/reference/cli-generated.md` → *Agent surface*.

## 14. Automatic onboarding — one project per model (CLI · Dashboard · Jupyter)

Instead of wiring each project by hand, **onboard a model** to get its whole workspace in one idempotent
step: the project envelope + quota, per-project storage, a **bound per-project MinIO/S3 connection**,
a FinOps budget, the model assignment, a Jupyter workbench, and the train/serve pipeline surfaces. All
three surfaces call the *same* code path (`examlops.modelzoo_adopt`), so they can never drift.

The MinIO connection resolves its endpoint/keys from the platform S3 env (`MLFLOW_S3_ENDPOINT_URL` +
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`); the secret is stored in the secrets store and never
printed. If no S3 endpoint is configured the connection step is recorded as `skipped` (the rest still
provisions); pass `--no-connection` to skip it deliberately.

**CLI**
```bash
exa modelzoo adopt JPCP                 # one project for JPCP, with a bound "minio" connection
exa modelzoo adopt --all                # backfill every Zoo/pack model (idempotent)
exa modelzoo adopt --all --dry-run      # preview what each model would provision
exa modelzoo adopt JPCP --no-connection # project only, skip the MinIO wiring
exa modelzoo adopt JPCP --connection-name minio-prod   # rename the per-project connection
```

**Jupyter / SDK** — the stable programmatic surface (`examlops.sdk`):
```python
from examlops.sdk import list_zoo_models, onboard_model, onboard_all_models

list_zoo_models()                       # ['JPCP', 'MACK', 'MCBound']
onboard_model("JPCP")                   # {'project':'jpcp','changed':True,'steps':{...}}
onboard_all_models(dry_run=True)        # preview every model, writes nothing
```

**Dashboard** — the Projects console has a **Sync Model Zoo** action (admin / `project.manage`), which
provisions a project for every Zoo model and reports how many were newly created. Backing routes:
`GET /api/v1/projects/zoo-models`, `POST /api/v1/projects/onboard/{model}`,
`POST /api/v1/projects/onboard-all` (viewer → 403; every real change audited as a `dashboard` event).

Each result reports every step as `created` / `exists` / `skipped` (or the `would-*` forms under
dry-run), so a re-run is safe and shows exactly what (if anything) changed.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_PROJECT` | unset | Active project override (wins over `config.toml`). Set by `exa project use`. |
| `EXAMLOPS_MULTITENANCY` | unset (off) | Enables RBAC enforcement of project membership. |
| `EXAMLOPS_ACTOR` | `$USER` | Actor recorded in audit events for project mutations. |
| `PLATFORM_DB` | `./platform.db` | Shared SQLite DB holding `projects`, `project_resources`, `project_models`, `project_budgets`. |

## Related

- `docs/reference/cli-generated.md` — full `exa project` command tree
- `docs/guides/rbac-multi-tenancy.md` — how membership grants are enforced
- ADRs 0086 (unified workspace), 0087–0090 (connections, project-scoped serving/pipelines,
  project FinOps, workbenches), 0084 (Docker resource envelopes), 0057 (authn/authz/multitenancy)
