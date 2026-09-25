# RBAC & multi-tenancy

ExaMLOps authorizes access with a **relationship model** (`owner ⊇ editor ⊇ viewer`)
over objects — models, datasets, prompts, secrets, deployments — with a **default-deny**
policy. Projects are the tenant boundary.

Design: ADR 0014 · spec `design/vision/specs/D6-rbac-multi-tenancy.md`. Backed by
`platform_db.authz_relations`; an [OpenFGA](#openfga-backend-optional) server can answer instead.

## Feature flag — single-tenant stays unchanged

Multi-tenant enforcement is behind `EXAMLOPS_MULTITENANCY`. **Unset/false → every
`check()` allows**, so existing single-tenant deployments behave exactly as before
(spec R9). Set it truthy to turn on default-deny enforcement.

## Model

- **Relations**: `owner` (full control), `editor` (retrain/update), `viewer` (read).
  A stronger relation satisfies any weaker requirement.
- **Objects** are hierarchical strings: `project:acme/model:JPCP`. A grant on the parent
  `project:acme` covers its children — so a project owner owns its models and datasets.
- **Default deny**: no relation → no access; denials are audited (D4).

## CLI

```bash
exa project grant alice owner  project:acme                   # alice owns everything in acme
exa project grant bob   editor project:acme/model:JPCP        # bob can retrain JPCP only
exa project revoke bob  editor project:acme/model:JPCP
exa project access --object  project:acme                     # who can access this object
exa project access --subject alice                            # what can alice access
```

## Enforcing in code

```python
from examlops import authz

authz.require(user, "editor", f"project:{proj}/model:{model}")   # raises PermissionError on deny
if authz.check(user, "viewer", f"project:{proj}/dataset:{ds}"):
    ...
```

Enforcement points: dashboard API, control-plane, the `exa` CLI (service identity), and
the B2 gateway. Secrets (D7), gateway keys (B2), and audit scope (D4) are partitioned by
project.

## Identity

Production multi-tenant mode authenticates via **OIDC** (Keycloak default; any OIDC
provider), issuing short-lived tokens carrying subject + group claims; shared dashboard
passwords are disabled in that mode.

## Who is refused, and where

With `EXAMLOPS_MULTITENANCY` on, every `exa project` command and every dashboard project route asks
`examlops.authz.guard.allowed(subject, relation, project)`:

| Needs | CLI (`exa project ...`) | Dashboard (`/api/v1/projects/...`) |
|---|---|---|
| viewer | `show`, `members`, `pipelines`, `cost`, `budget`, `compose`; `list` shows only your projects | `GET /{name}`; `GET` list is filtered |
| editor | `set-quota`, `assign`, `assign-model`, `storage` | `PUT /{name}`, `POST /{name}/resources`, `POST /{name}/storage` |
| owner | `add-member`, `remove-member`, `archive`, `delete`, `grant`, `revoke` | `POST/DELETE /{name}/members`, `DELETE /{name}` |

A refusal is exit code 1 (`Permission denied: ...`) on the CLI and HTTP 403 on the dashboard, happens
before any lookup (a stranger learns nothing about whether a project exists), changes nothing, and is
audited as `authz_deny`. A backend failure denies. The CLI subject is `EXAMLOPS_ACTOR`/`$USER`
(after `exa auth login`, the federated user).

- **Creator becomes owner:** `exa project create` / `POST /projects` grants the creator `owner`.
- **Bootstrap admins:** `EXAMLOPS_AUTHZ_ADMINS=alice,ops` may act on every project.
- **Legacy password sessions** (no per-user identity) are the subjects `legacy:admin`,
  `legacy:operator` and `legacy:viewer`, holding owner / editor / viewer on project `default` only
  (the ADR 0014 migration). Give them more explicitly:
  `exa project grant legacy:admin owner project:research`.
- **Flag off:** everything is allowed, exactly as before.

### Models and datasets, outside `exa project`

A model or dataset belongs to every project that lists it in `project_resources`
(`exa project assign <p> <name> --kind model|dataset`; models also through the older
`project_models`, and through `exa namespace assign` - a namespace is the project of the same
name, as it already is for budgets; the `default` namespace claims nothing). Names are matched case-insensitively (`JPCP` and `jpcp` are the same model). A
model or dataset **no project has claimed belongs to `default`**, so the legacy migration grants
cover it and nobody else reaches it.

The commands and routes that act on one ask
`examlops.authz.guard.resource_allowed(subject, relation, kind, name)`: the subject needs the
relation on **every** project that holds it (a model shared by two projects is changed for both,
so both must agree). A relation on `project:<p>` or on the object itself
(`project:<p>/model:<m>`) counts.

| Needs | CLI | Control plane |
|---|---|---|
| viewer | `serve traffic <m>` (show), `pipeline promote <m> --dry-run`, `data list <ds>`, `data diff`, `data checkout`, the `--dataset` of `pipeline run` | — |
| editor | `pipeline promote <m>`, `pipeline run --model <m>` (and `editor` on `--project <p>`), `serve traffic <m> --production …`, `drift auto-retrain enable <m>` / `disable`, `data snapshot <ds>`, `data validate <ds>`, `data synth generate <ds>`, `cards dataset <ds>`, `project assign <p> <m|ds>` (on the resource's *current* project(s), as well as `editor` on `<p>`) | `POST /retrain`, `/v1/retrain`, `/approve/{m}`, `/reject/{m}`, `/api/changes` (every model it names), `DELETE /approvals/{id}` and `/v1/commands/{id}` (the row's model) |

`exa pipeline run` with no `--model` trains the whole registry; with multi-tenancy on only a
platform admin (`EXAMLOPS_AUTHZ_ADMINS`) may do that. Because membership *is* the authorization
key, attaching a model or dataset to a project (`exa project assign`, the dashboard's
`POST /api/v1/projects/{p}/resources`) needs `editor` where it lives now - otherwise any project
owner could claim another project's model, or every unassigned one away from `default`. `cplane.project_gate.ROUTE_PROJECT` classifies
every mutating control-plane route (the webhooks and the `admin`-scope routes are exempt, each with
its reason), and a test fails when a new route is not classified.

**Who the control plane's caller is:** the shared `CONTROL_PLANE_TOKEN` is `legacy:operator`
(editor on `default` only); a static or workload credential is its configured `principal` — grant
it (`exa project add-member acme dataplane-bus-bridge --role editor`) or list it in
`EXAMLOPS_AUTHZ_ADMINS`; a federated IdP token is its subject, and a project role the verified token
asserts counts too (`operator` = editor, `admin` = owner).

**Refusals:** CLI exit 1 before any lookup or side effect; control plane HTTP 403 before any lookup,
audited `authz_deny`. If the membership tables cannot be read the answer is a refusal (CLI exit 1,
HTTP 503) and an `authz_error` event — an outage never opens a scoped model. The control plane
counts every decision in `control_plane_project_authz_decisions_total{outcome="allow|deny|unavailable"}`.

> **Before turning the flag on**, grant each service principal that retrains or reports changes
> (the Dataplane bus bridge, the autopilot, CI) `editor` on the projects whose models it serves, or
> make it a platform admin — otherwise its retrains are refused with 403.

## OpenFGA backend (optional)

Set `EXAMLOPS_OPENFGA_URL` and `EXAMLOPS_OPENFGA_STORE_ID` (optionally `_MODEL_ID`, `_TOKEN`,
`_TIMEOUT`) and `check`, `grant` and `revoke` go to OpenFGA over HTTP (`httpx`, no new dependency).
Nothing changes unless both are set; URL without a store id falls back to the native table with an
ERROR log. **Fail closed:** a timeout, connection error, non-2xx reply or malformed body is a deny
(and an `authz_error` audit event); a grant or revoke OpenFGA refuses is not applied natively either.
Objects map as `project:acme` -> `project:acme` and `project:acme/model:JPCP` -> `model:acme~JPCP`
plus a `parent` tuple (every check on a child sends that link as a *contextual tuple*, so a
project-level grant reaches a child nobody granted on directly); upload the model from `examlops.authz.openfga` (`fga model write`), then
backfill existing grants with `exa project openfga-sync --execute`. Object types the exported model
does not define (e.g. `platform:core`) are rejected by OpenFGA and therefore denied.

## Is every table scoped to a project?

Not yet, and the platform says so: `exa project scope-audit` (read-only) classifies every
`platform.db` table as scoped (a `tenant`/`project` column), model-scoped (reachable via the model's
project), dataset-scoped (reachable via the dataset's project: `dataset_revisions`,
`dataset_cards`, `synthetic_datasets`, and `data_retention`, which no
code writes yet; the `exa data` / `data synth generate` / `cards dataset` guards above enforce
it for the commands that write or read them), exempt (with a
kind and reason: global, security, root, child) or **UNSCOPED**, and lists the `known_gaps` —
user-data tables such as `prompt_versions` or `feature_views` that are not yet partitioned by project.
`tests/unit/test_scope_audit.py` fails the build when a new table is none of these.
