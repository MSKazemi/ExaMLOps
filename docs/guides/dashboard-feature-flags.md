# Feature Flags & Staged Rollout

The dashboard has a self-hosted feature-flag layer. Flags are evaluated **server-side** with the
caller's context (tenant, role, and a deterministic percentage bucket); the client receives
*decisions*, not rules. Admins manage flags from the **Flags** page, and every change is audited.

- **Feature:** F25 · **Design:** [ADR 0070](../../design/adr/0070-dashboard-feature-flags-staged-rollout.md) ·
  **Spec:** `design/vision/specs/F25-feature-flags-staged-rollout.md`
- **Backend:** `platform/services/dashboard/backend/feature_flags.py` + `routers/flags.py`
- **Frontend:** `platform/services/dashboard/frontend/src/lib/serverflags.ts` +
  `lib/flags.ts` (F23 client defaults) + `pages/Flags.tsx`

## Using a flag in the UI

```tsx
import { useFlag } from '@/lib/serverflags'

function MyFeature() {
  if (!useFlag('incidentTimeline')) return null
  return <Timeline />
}
```

`useFlag(name)` prefers the **server decision** (`GET /api/v1/flags`) and falls back to the client-side
default in `lib/flags.ts` if the decisions payload hasn't arrived — so the UI degrades gracefully.

## Evaluation order (server-side)

For each flag the backend applies, in order:

1. **default** (from the registry) — or an **admin override** if set (override wins).
2. **role targeting** — if `roles` is set and the caller's role isn't in it → off.
3. **tenant targeting** — if `tenants` is set and the caller's tenant isn't in it → off.
4. **percentage rollout** — if `percentage < 100`, include the subject when
   `subject_bucket(flag, subject) < percentage`. The bucket is a stable SHA-256 hash, so a subject
   always lands on the same side (R5). **Admins bypass** percentage gating (so they can test a
   rollout).

## Admin (Flags page)

Admins see every flag with its default, override, effective state, and targeting, and can toggle it.
`POST /api/v1/flags/{name}`:

1. Persists the override to `feature_flag_overrides` in `platform.db`.
2. **Audits** the change to `audit_events` (`source = "dashboard-flags"`, action `flag_set`, D4).
3. **Publishes** `event.flag_changed` on the F8 realtime channel (live kill-switch delivery).

## Endpoints

| Endpoint | Role | Purpose |
|---|---|---|
| `GET /api/v1/flags` | viewer | Server-evaluated decisions for the caller |
| `GET /api/v1/flags/admin` | admin | Definitions + overrides + effective state |
| `POST /api/v1/flags/{name}` | admin | Set an override (audited + published) |

See [`docs/reference/api.md`](../reference/api.md) for shapes and
[`docs/dashboard/architecture.md`](../dashboard/architecture.md#feature-flags--staged-rollout-f25) for
the diagram.

## Notes & limits

- Overrides live in `feature_flag_overrides` (created on first write); the registry lives in
  `feature_flags.py`. No third-party SaaS.
- This slice ships server-side evaluation, staged rollout, and the audited admin toggle. Deferred:
  **live push** of decisions over F8 into `useFlag` (currently poll + invalidate on change), a full
  targeting **CRUD editor**, and per-flag **adoption telemetry** to F24 — tracked in the plan.
