# Dashboard Self-Observability

The dashboard observes **itself**: a **Status** page shows its own dependency health and request
metrics, and UI actions are audited to the platform database. This core has **no third-party
telemetry egress** — everything is self-hosted.

Open the status page from the sidebar (**Status**) or navigate to `/status`.

- **Feature:** F24 · **Design:** [ADR 0067](../../design/adr/0067-dashboard-self-observability.md) ·
  **Spec:** `design/vision/specs/F24-dashboard-self-observability.md`
- **Backend:** `platform/services/dashboard/backend/selfobs.py` + `routers/selfobs.py`
- **Frontend:** `platform/services/dashboard/frontend/src/lib/telemetry.ts` + `pages/SelfObs.tsx`

## Status page (R5)

Shows, auto-refreshing every 15s:

- **KPI tiles** (reusing the F4 `<KpiTile>`): request count, server errors (threshold-coloured),
  rate-limit hits, and p95 latency.
- **Dependencies** — each with an `up`/`degraded`/`down` pill and latency. `platform_db` is probed
  live; `bff` is the dashboard itself.

Metrics come from `MetricsMiddleware`, which records every response's status class and latency into an
in-process collector (`selfobs.METRICS`).

## UI-action audit (R4 / D4)

Sensitive UI actions are recorded to `platform_db.audit_events` (`source = "dashboard-ui"`):

```ts
import { reportAction } from '@/lib/telemetry'
await reportAction('open_page', '/mlops')   // best-effort; never breaks the UX
```

The details field is **PII-scrubbed client-side** before it is sent.

## PII scrubbing (R1 / F16)

`scrubPii()` removes emails, `Bearer` tokens, JWTs, and long hex ids from any string before it would
be reported. It is pure and unit-tested — the privacy-sensitive part:

```ts
scrubPii('contact alice@example.com Bearer abc.def')  // "contact [email] Bearer [redacted]"
```

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/selfobs/status` | Dependency health + self-metrics (status page) |
| `POST /api/v1/selfobs/action` | Audit a UI action to `platform_db` |

Both require the `viewer` role. See [`docs/reference/api.md`](../reference/api.md) for shapes and
[`docs/dashboard/architecture.md`](../dashboard/architecture.md#self-observability-f24) for the diagram.

## Deferred (tracked in the dashboard-nextgen plan)

- **GlitchTip** self-hosted JS error tracking with source-mapped, release-tagged, PII-scrubbed stacks (R1).
- **OTel browser tracing** correlated through the F8 BFF into a single Tempo trace (R2).
- Self-hosted, anonymized, consent-aware **product analytics** (PostHog/Umami) — no third-party (R3).
- **web-vitals** (LCP/INP/CLS) → Prometheus/Grafana RUM (R4).
- **Playwright synthetic monitoring** of critical flows on a schedule → F12 alerts on failure (R6).
