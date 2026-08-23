# Alerting & Incidents

The **Alerts** page is a unified inbox of active alerts derived from the platform's own signals —
prediction drift, budget overspend, and eval regressions. Alerts are severity-coded, and
acknowledging one is audited.

Open it from the sidebar (**Alerts**) or navigate to `/alerts`.

- **Feature:** F12 · **Design:** ADR 0062 (`design/adr/0062-dashboard-alerting-incident-oncall.md`) ·
  **Spec:** `design/vision/specs/F12-alerting-incident-oncall.md`
- **Backend:** `platform/services/dashboard/backend/alerts.py` + `routers/alerts.py`
- **Frontend:** `platform/services/dashboard/frontend/src/lib/alerts.ts` + `pages/Alerts.tsx`

## Alert sources (R1)

| Source | Fires when… | Severity |
|---|---|---|
| `drift` | latest prediction mean is ≥3σ (or ≥2σ) from the model's drift baseline | critical / warn |
| `budget` | a project's consumed cost exceeds its `cost_budget` | error |
| `eval` | the latest eval run for a model has a failed metric | warn |

Alerts are sorted most-severe first and carry a deterministic `id` (`source:key`, e.g. `drift:jpcp`).
The header shows a headline summary (e.g. "1 critical · 2 warnings").

## Acknowledging (R3)

Click **Ack** on an alert. This calls `POST /api/v1/alerts/{id}/ack`, which:

1. **Audits** the acknowledgement to `platform_db.audit_events` (`source = "dashboard-alerts"`, D4).
2. **Publishes** `alert.acked` on the F8 realtime channel, so other open dashboards update live.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/alerts` | The unified alert inbox |
| `POST /api/v1/alerts/{id}/ack` | Acknowledge an alert (audited + published) |

Both require the `viewer` role. See [`docs/reference/api.md`](../reference/api.md) for shapes and
[`docs/dashboard/architecture.md`](../dashboard/architecture.md#alerting-incident-f12) for the diagram.

## Notes & limits

- Reads from `platform.db` (`PLATFORM_DB`); missing tables degrade to an empty inbox, never an error.
- This slice ships the derived inbox + ack. The richer F12 surfaces — Alertmanager merge + **incident**
  correlation/timeline (R2), full silence/snooze (R3), **on-call/escalation** + notification channels
  (R4), inline **runbooks** + gated remediation (R5), and the **SLO/error-budget** burn-rate board
  (R6) — build on this and are tracked in the dashboard-nextgen plan. The alert **grid** lands with F17.
