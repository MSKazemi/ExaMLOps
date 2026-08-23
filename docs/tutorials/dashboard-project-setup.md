# Tutorial — Set up a project from the Dashboard

This walkthrough configures a complete **project workspace** — a project, a data connection, its
storage, and its people — entirely from the web dashboard, no CLI required. It exercises the *edit
parity* added in Phase 42: the dashboard's write actions call the **same `examlops.*` code the `exa`
CLI uses**, so anything you set up here is identical to (and interoperable with) the CLI, and any
secret you enter is stored in the CLI/serving-readable secrets store.

> Every action below is **admin-only** (`project.manage` / `connection.manage`). Log in with the
> admin password (`DASHBOARD_ADMIN_PASSWORD`). A viewer sees the same pages read-only — the write
> controls are hidden and the BFF returns `403` with a human explanation if called directly.
>
> The CLI equivalent is shown under each step so you can see the parity.

## Prerequisites

- The dashboard running (`make dashboard-up`, or the [local-dev recipe](../dashboard/operations.md#running-it-without-docker)).
- The `projectsConsole` feature flag enabled (default on).
- `PLATFORM_DB` pointing at the same `platform.db` the CLI uses (docker-compose does this).

## 1. Create the project

Open **Projects** → **Create**. Give it a name (e.g. `research`) and, optionally, quota
(CPU / memory / storage / GPU). Save.

```bash
# CLI equivalent
exa project create research --cpu-limit 4 --memory-gb 8 --storage-gb 100
```

You land on the project detail page — the single anatomy pane (Quota · Budget · Storage · Pipelines ·
Resources · Members · Connections · Workbenches).

## 2. Add a data connection

In the **Connections** card, click **New connection**:

- **Name** — e.g. `minio-data`
- **Kind** — `s3` (or `uri` / `dataplane`)
- **Config (JSON, non-secret)** — e.g.
  `{"endpoint":"http://localhost:19000","bucket":"data","access_key":"minioadmin"}`
- **Secret (optional)** — the credential. It is written through `examlops.secrets` (encrypted at rest,
  CLI/serving-readable) and **never** returned to the browser afterwards — the row shows only
  *secret set*.

Save. Use the per-row **Test** button to run a reachability probe.

```bash
# CLI equivalent
exa connection create minio-data --kind s3 --project research \
    --config '{"endpoint":"http://localhost:19000","bucket":"data","access_key":"minioadmin"}' \
    --secret-value minioadmin
exa connection test minio-data --project research
```

## 3. Provision and bind storage

In the **Storage** card, click **Provision storage** to create the per-project MinIO layout
(`s3://$EXAMLOPS_PROJECTS_BUCKET/<project>/{artifacts,datasets,cache}/`). To point storage at a
dedicated bucket, open the modal again and pick the connection from step 2 under **Bind connection**.

```bash
# CLI equivalent
exa project storage research                          # provision
exa project storage research --bind-connection minio-data
```

## 4. Add and manage members

In the **Members** card, **Add member** (subject + role: owner ⊇ editor ⊇ viewer). Each member has a
**Remove** button. Roles are stored as D6 `authz_relations` — the same model the CLI writes.

```bash
# CLI equivalent
exa project add-member research alice --role editor
exa project members research
```

## 5. See the audit trail

Every action above wrote a `dashboard`-sourced event. View it in the dashboard **Audit** page or:

```bash
exa audit --last 1d
```

You'll see `project_created`, `connection_created`, `project_storage_bound`, `project_member_added`,
each with the actor (your admin login) and target.

## 6. Tear down (optional)

The project detail page has a **Danger zone** → **Delete project**. This removes the project grouping
and its membership/resource/storage/pipeline rows — but **not** the underlying models or connections
(those are independent resources).

```bash
# CLI equivalent — the connection outlives the project unless you delete it too
exa connection delete minio-data --project research
```

## What just happened (how it works)

- The dashboard write routers (`routers/connections.py`, `routers/projects.py`) call the shared
  `examlops.*` functions rather than duplicating SQL — so the CLI and dashboard are one implementation.
- The BFF is the sole enforcement point: viewers are read-only; admins hold `project.manage` /
  `connection.manage`; secrets never cross the wire.
- See the [Projects & Workspaces guide](../guides/projects-workspaces.md) §8, the
  [architecture view](../architecture/project-workspace.md) §7 (CLI↔Dashboard parity), and the
  [auth & tenancy guide](../guides/dashboard-auth-tenancy.md) for the capability model.
