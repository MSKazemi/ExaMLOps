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

## OpenFGA backend (optional)

Set `EXAMLOPS_OPENFGA_URL` and `EXAMLOPS_OPENFGA_STORE_ID` (optionally `_MODEL_ID`, `_TOKEN`,
`_TIMEOUT`) and `check`, `grant` and `revoke` go to OpenFGA over HTTP (`httpx`, no new dependency).
Nothing changes unless both are set; URL without a store id falls back to the native table with an
ERROR log. **Fail closed:** a timeout, connection error, non-2xx reply or malformed body is a deny
(and an `authz_error` audit event); a grant or revoke OpenFGA refuses is not applied natively either.
Objects map as `project:acme` -> `project:acme` and `project:acme/model:JPCP` -> `model:acme~JPCP`
plus a `parent` tuple; upload the model from `examlops.authz.openfga` (`fga model write`), then
backfill existing grants with `exa project openfga-sync --execute`. Object types the exported model
does not define (e.g. `platform:core`) are rejected by OpenFGA and therefore denied.

## Is every table scoped to a project?

Not yet, and the platform says so: `exa project scope-audit` (read-only) classifies every
`platform.db` table as scoped (a `tenant`/`project` column), model-scoped (reachable via the model's
project), exempt (with a kind and reason: global, security, root, child) or **UNSCOPED**, and lists the
`known_gaps` — user-data tables such as `dataset_revisions` that are not yet partitioned by project.
`tests/unit/test_scope_audit.py` fails the build when a new table is none of these.
