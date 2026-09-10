# Dashboard AuthZ & Multi-Tenancy

The dashboard authorizes actions with a **capability model** and scopes data by **tenant**. The
backend (BFF) is the sole enforcement point; the UI uses capabilities only to decide what to
show/disable and to explain why.

- **Feature:** F15 · **Design:** ADR 0057 (`design/adr/0057-dashboard-authn-authz-multitenancy.md`) ·
  **Spec:** `design/vision/specs/F15-authn-authz-multitenancy.md`
- **Backend:** `platform/services/dashboard/backend/capabilities.py`
- **Frontend:** `platform/services/dashboard/frontend/src/lib/capabilities.ts`,
  `components/CapabilityGate.tsx`

## Capabilities

Each role maps to a capability set:

| Capability | viewer | admin |
|---|:---:|:---:|
| `view`, `search` | ✅ | ✅ |
| `model.promote` | — | ✅ |
| `approval.decide` | — | ✅ |
| `retrain.trigger` | — | ✅ |
| `drift.baseline` | — | ✅ |
| `config.write` | — | ✅ |
| `secret.reveal` | — | ✅ |
| `service.control` | — | ✅ |
| `project.manage` | — | ✅ |
| `connection.manage` | — | ✅ |
| `cli.run` | ✅ | ✅ |
| `cli.write` | — | ✅ |

`cli.run` lets anyone signed in run **read**-tier `exa` commands in the CLI Console; `cli.write`
gates every command that changes state (admin and destructive tiers, and a read whose arguments
persist, name a file or reach another host) plus the CLI workspace — see
[CLI Console](dashboard-cli-console.md) (ADR 0119).
`project.manage` gates project create/delete, resource assign, and member add/remove;
`connection.manage` gates connection create/test/delete and project storage binding. Both write paths
call the same `examlops.*` code the `exa` CLI uses, so a secret entered in the dashboard is stored in
the CLI-compatible secrets client and only `hasSecret` (never the value) is returned to the browser.

`GET /api/auth/me` returns the caller's `role`, `tenant`, and `capabilities[]` — the UI's affordance
source.

### Enforcing on the backend

Guard a mutating route with the capability dependency (the BFF is the sole enforcement point):

```python
from capabilities import require_capability, MODEL_PROMOTE

@router.post("/promote/{name}")
async def promote(name: str, _=Depends(require_capability(MODEL_PROMOTE))):
    ...   # viewer → 403 with an explanation; admin → allowed
```

### Affording on the frontend

```tsx
const { can, reason } = useCapabilities()
can('model.promote')          // boolean
reason('model.promote')       // "" if allowed, else a human explanation

// Or wrap a control — dimmed + explained when denied (never a silent dead control, R3):
<CapabilityGate capability="model.promote">
  <PromoteButton />
</CapabilityGate>
```

## Step-up / MFA (R6)

`model.promote` and `secret.reveal` are flagged as **step-up** actions (`requiresStepUp()` /
`STEP_UP_CAPABILITIES`). The flag is surfaced so the UI can prompt and the audit trail can record it;
BFF-enforced step-up/MFA is deferred (see below).

## Tenant scoping (R4, default-deny)

A principal sees only its own tenant's resources. Helpers in `capabilities.py`:

- `tenant_visible(principal, resource_tenant)` — same tenant, or a **cross-tenant admin**.
- `assert_tenant_access(principal, resource_tenant)` — **403** on a cross-tenant request (GWT-3).
- `scope_to_tenant(principal, rows)` — filter a list (rows without a tenant are `"default"`).

Tokens without a tenant claim default to `"default"` (the current single-tenant deployment). The
shell shows a tenant badge once a non-default tenant is in scope.

## Verifying

```bash
cd platform/services/dashboard/backend && python -m pytest tests/test_capabilities.py -q
cd platform/services/dashboard/frontend && npx vitest run src/lib/capabilities.test.ts
```

## Deferred (tracked in the dashboard-nextgen plan)

- **OIDC/Keycloak** Authorization-Code + PKCE replacing the shared-password JWT; tenant + relationship
  claims in the token (R1).
- **OpenFGA** relationship checks as the enforcement backend (R2).
- A **tenant switcher** for multi-tenant admins + per-entity `tenant` columns so lists rescope (R4).
- **Service accounts / scoped API tokens** + session listing/revocation + idle timeout (R5).
- **Step-up/MFA** enforcement for governed actions, expressed as policy (R6).
