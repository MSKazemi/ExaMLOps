# RBAC & multi-tenancy

ExaMLOps authorizes access with a **relationship model** (`owner ⊇ editor ⊇ viewer`)
over objects — models, datasets, prompts, secrets, deployments — with a **default-deny**
policy. Projects are the tenant boundary.

Design: ADR 0014 · spec `design/vision/specs/D6-rbac-multi-tenancy.md`. Backed by
`platform_db.authz_relations` (an OpenFGA backend can be swapped behind `authz.check`).

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
