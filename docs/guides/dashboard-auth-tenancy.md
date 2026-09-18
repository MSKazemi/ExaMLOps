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

Each role maps to a capability set. `operator` (ADR 0120) runs the model lifecycle without holding
the platform's keys; it is reached through organisation SSO (a center's group mapped to it), since
the shared-password login knows only viewer and admin.

| Capability | viewer | operator | admin |
|---|:---:|:---:|:---:|
| `view`, `search` | ✅ | ✅ | ✅ |
| `model.promote` | — | ✅ | ✅ |
| `approval.decide` | — | ✅ | ✅ |
| `retrain.trigger` | — | ✅ | ✅ |
| `drift.baseline` | — | ✅ | ✅ |
| `traffic.manage` | — | ✅ | ✅ |
| `config.write` | — | — | ✅ |
| `secret.reveal` | — | — | ✅ |
| `service.control` | — | — | ✅ |
| `project.manage` | — | — | ✅ |
| `connection.manage` | — | — | ✅ |
| `cli.run` | ✅ | ✅ | ✅ |
| `cli.write` | — | — | ✅ |

How users sign in — organisation SSO against the data center's IdP, IdP bearer tokens for API
clients, or the break-glass passwords — is covered in [Identity federation](identity-federation.md).
For a federated user the center's own policy engine may additionally veto a capability.

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

## Reaching the gate

`require_capability(cap)` is the **enforcing** dependency. It refuses a role that lacks the
capability *and then* calls `iam_gate.enforce`: RFC 9470 step-up for the designated capabilities,
and the centre's **PDP** asked about *that named capability*.

Two PDP questions, at different granularities, and the difference is the whole point:

| Asked by | On | Question |
|---|---|---|
| `require_role` → `center_route_check` | **every** authenticated route | `api.read` / `api.write` on the route path |
| `require_capability` → `enforce` | routes that use it | the **capability** itself, e.g. `model.promote` |

So a federated caller is never unchecked. But a centre whose policy is written in capability terms —
which is how ADR 0120's own example writes one — only bites where the fine-grained question is
asked.

Checking a capability with a bare `can(role, cap)` therefore reuses the *name* of a gate without the
gate. `routers/challenger.py` did that for `model.promote`, a step-up capability its own docstring
described as reusing the established gate: with step-up enforced, `PUT /api/models/…/alias` demanded
re-authentication while `POST /api/challenger/{model}/promote` — the same model, promoted to
production — did not. Fixed 2026-09-13 by adding the enforcing dependency **alongside** the existing
admin one, rather than swapping it: `require_capability` admits operators too, and widening who may
promote was not that fix's business.

**Writing a route that checks a capability:** use `Depends(require_capability(CAP))`. Keep any role
dependency you already have if the route should stay narrower than the capability alone.

`tests/unit/test_capability_gate_is_reached.py` holds two rules of different force: a route guarding
a **step-up** capability with a bare `can()` fails the build outright, and any router that still
checks another capability that way is listed so the number can only fall.

**That list started at 20 and is now one.** 47 write routes across 18 routers were converted on
2026-09-13, so a centre policy expressed as `secrets.manage`, `project.manage`, `traffic.manage`,
`drift.baseline` and the rest now reaches the routes that use them. The conversions add the
dependency **alongside** each route's existing role dependency rather than replacing it —
`require_capability` admits operators, so a swap would widen who may act while looking like a
tightening.

**The list is now empty.** The last entry, `cli.py`, needed a different shape rather than the same
edit, and got one. Its capability is chosen **per request** — `cli.run` for a read-tier `exa`
command, `cli.write` for one that changes platform state — and is only known after the argv has been
built, so there is nothing to name in a route-level dependency. It calls `iam_gate.enforce` itself,
once the answer exists.

That matters for policy: before it, a centre saw only the coarse `api.write` for
`/api/v1/cli/runs` whatever command was in the body, so it could not permit read-only `exa` use
while forbidding the mutating kind — which is the entire reason there are two capabilities. Both
halves are now tested against a real PDP.

So there are **two legitimate shapes**, and the guard accepts both: a route-level
`Depends(require_capability(CAP))` where the capability is static, and a direct `enforce(...)` call
where it is not. A router using neither reaches no capability-level check at all, and fails the
build.

## Step-up / MFA (R6)

`model.promote` and `secret.reveal` are **step-up** actions (`requiresStepUp()` /
`STEP_UP_CAPABILITIES`). The BFF enforces them (RFC 9470, `iam_gate.py`) **when the deployment opts
in**: a federated user's center has a `step_up` entry in the trust file (e.g. require the REFEDS MFA
`acr` within 15 minutes), or `EXAMLOPS_IAM_STEP_UP=enforce` for local password sessions. Unmet, the
BFF answers `401 insufficient_user_authentication` with `acr_values`/`max_age` and the UI sends the
user back through the center's login. Not opted in, these are permitted on the capability check.

## Tenant scoping (R4, default-deny)

A principal sees only its own tenant's resources. Helpers in `capabilities.py`:

- `tenant_visible(principal, resource_tenant)` — same tenant, or a **cross-tenant platform admin**.
  An admin whose identity comes from a data center's IdP is confined to that center's tenant.
- `assert_tenant_access(principal, resource_tenant)` — **403** on a cross-tenant request (GWT-3).
- `scope_to_tenant(principal, rows)` — filter a list (rows without a tenant are `"default"`).
- `tenant_sql_filter(principal)` — the same rule as a SQL predicate plus its parameters, for a
  query that carries its own `LIMIT`. Returns `("1=1", ())` for a cross-tenant admin, so it can
  be interpolated unconditionally.

Tokens without a tenant claim default to `"default"` (the current single-tenant deployment) — **on
both sides of the comparison**. Only the resource side defaulted that way until 2026-09-13, and
routes are handed the raw token payload, which for a locally issued token has no `tenant` claim. So
`None != "default"`, the filter denied everything, and wiring it into a route emptied the page.

### Which routes are scoped, and which are not yet

This section described the scoping as enforced for two years while
`assert_tenant_access` and `scope_to_tenant` had **no callers at all** — 62 routers, zero uses. The
control existed, was unit-tested, and governed nothing, partly because of the bug above: switching
it on looked like it broke the page.

`GET /api/slo` was the proof. It selected every row of `slo_specs` with no `WHERE` and bound the
principal to `_`, so every viewer of every tenant saw every other tenant's SLO definitions —
including `sli_query`, a Prometheus expression carrying that tenant's metric and label names.
`GET /api/secrets` did the same for secret *paths* (never values) and `GET /api/gateway/keys` for
virtual-key projects, budgets and spend.

Those three, and the four found alongside them (`compliance_systems`, `fairness_config`,
`inference_gateway_config`, `scale_events`), are all scoped now.
`tests/unit/test_dashboard_tenant_scoping.py` holds that state: a route that reads one of the 31
tenant-scoped tables without scoping it fails the build. The scan keeps itself honest against a
planted router, so it cannot go quiet by matching nothing.

**Two ways to read another tenant's rows, and the guard checks both.** The first is raw SQL with no
`WHERE`. The second is subtler and the SQL scan is blind to it: several `examlops.data` helpers take
`tenant: str | None = None`, where omitting it means *every tenant*. `GET /api/challenger` called
`list_challenger_configs()` exactly that way, and nothing flagged it — a helper whose default is "no
filter" reads like a helper with a safe default. Pass a tenant, or scope the result.

!!! danger "Three per-model registers are single-tenant, whatever the tenant column says"

    `compliance_systems`, `fairness_config` and `challenger_config` each carry a `tenant` column
    and are keyed `PRIMARY KEY(model)`. A model name is not unique across tenants, and the writers
    find the row by model alone — so a second tenant declaring the same model name does not get its
    own row and does not get an error. **It overwrites the first tenant's row and reassigns the
    tenant to itself.** Demonstrated:

    ```python
    set_compliance_system("JPCP", tenant="centre-a", risk_tier="high")     # A: high-risk
    set_compliance_system("JPCP", tenant="centre-b", risk_tier="minimal")  # B: its own model
    # one row: {"model": "JPCP", "tenant": "centre-b", "risk_tier": "minimal"}
    ```

    Centre A's EU-AI-Act declaration is gone. A cross-tenant read leaks; this destroys.

    **Until it is fixed, do not run more than one tenant against these three registers** — or make
    sure model names are globally unique across your tenants (prefix them per centre).

    The fix is identity `(model, tenant)` plus the helpers that look rows up by model, which on an
    existing install is a table rebuild — a **breaking** migration through `exa upgrade apply` that
    raises `min_reader_format` and locks older releases out of the data. That is a release
    decision, not a patch, so it is tracked rather than slipped in.
    `tests/unit/test_tenant_identity_keys.py` pins exactly which tables are affected (and which
    tenant-less keys are fine because they are surrogate ids) so the list cannot grow meanwhile.

**Writing a route that returns tenant-scoped rows:** bind the principal
(`principal: dict = Depends(_viewer)` — not `_`) and wrap the rows:

```python
return scope_to_tenant(principal, [dict(r) for r in rows])
```

The ratchet fails the build if you forget.

### A `LIMIT` must not run before the filter

`scope_to_tenant` filters rows **in Python**, which is correct only when the query returned every
row the caller might be entitled to. A query with its own `LIMIT` does not: the database takes the
newest N rows across *all* tenants and the filter then removes the ones belonging to others. The
caller is shown fewer of their own rows than exist — and once another tenant has N newer ones,
**none at all**, with an empty list that looks like "nothing happened".

`GET /api/nextgen/autoscale/events` was that shape until 2026-09-15. Put `tenant_sql_filter` in the
`WHERE` whenever the query is limited, and keep `scope_to_tenant` on the result: the predicate
narrows the query, the filter is the check.

```python
where, params = tenant_sql_filter(principal)
rows = _query(f"SELECT * FROM scale_events WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit))
return scope_to_tenant(principal, rows)
```

The guard in `tests/unit/test_dashboard_tenant_scoping.py` cannot see this: it asks whether a
router scopes *at all*, and this one did. What finds it is asking, per query, whether the rows the
filter receives are all the rows the caller is entitled to.

## Verifying

```bash
cd platform/services/dashboard/backend && python -m pytest tests/test_capabilities.py -q
cd platform/services/dashboard/frontend && npx vitest run src/lib/capabilities.test.ts
```

## Deferred (tracked in the dashboard-nextgen plan)

- ~~OIDC Authorization-Code + PKCE~~ — delivered by ADR 0120 as a backend-for-frontend with an
  HttpOnly session cookie; see [Identity federation](identity-federation.md).
- **OpenFGA** relationship checks as the enforcement backend (R2) — a center can already plug its own
  OpenFGA/OPA/AuthZEN PDP in through the trust file (ADR 0120).
- A **tenant switcher** for multi-tenant admins + per-entity `tenant` columns so lists rescope (R4).
- **Service accounts / scoped API tokens** + session listing/revocation + idle timeout (R5).
- ~~Step-up/MFA enforcement~~ — delivered (opt-in per center), see above.
