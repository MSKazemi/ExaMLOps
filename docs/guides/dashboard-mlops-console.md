# MLOps Console

The **MLOps Console** is the dashboard's single pane for model registry, lifecycle health, and
governed promotion. It surfaces the platform's already-shipped MLOps backend — drift, cost, serving
traffic, and promotion policy — so operators can see a model's state and whether it is safe to
promote without dropping to the CLI.

Open it from the sidebar (**MLOps**, the `Boxes` icon) or navigate to `/mlops`.

- **Feature:** F9 · **Design:** ADR 0060 (`design/adr/0060-dashboard-mlops-console.md`) ·
  **Spec:** `design/vision/specs/F9-mlops-console.md`
- **Backend:** `platform/services/dashboard/backend/mlops.py` + `routers/mlops.py`
- **Frontend:** `platform/services/dashboard/frontend/src/lib/mlops.ts` + `pages/MlopsConsole.tsx`

## What you see

### Registry grid

A row per model the platform knows about — composed from every model that has recorded drift, cost,
a serving-traffic rule, or a promotion policy. Columns:

| Column | Meaning |
|---|---|
| **Model** | Display name (uppercase, e.g. `JPCP`). The MLflow registry name (lowercase `jpcp`) is carried alongside for API calls. |
| **Ver** | Latest version seen in `model_costs` (`—` if no cost has been recorded). |
| **Stage** | Latest alias observed for the model (e.g. `Production`, `Staging`). |
| **Health** | Colour-blind-safe status token: `ok` (drift-tracked **and** governed), `warn` (one but not both), `unknown` (name only). Rendered as a `StatusPill` — never colour alone. |
| **Governed** | `Yes` when an **enabled** promotion policy exists, `No policy` otherwise. |
| **Freshness** | Most recent timestamp across the model's drift/cost/traffic records. |

Select a row to open its promotion gate on the right.

### Promotion gate (guided)

For the selected model, the gate shows — inline — whether a promotion is allowed and, when it is
**not**, exactly why:

- **Policy** — the configured rule (`promote Staging → Production when rmse < 5.0`). Missing or
  disabled policies are listed as blocking reasons.
- **Approval** — the phase-11 sysadmin approval step, always surfaced so the human gate is never
  hidden even for an otherwise-eligible model.

The one-line verdict (`Blocked — …`, `Eligible — awaiting approval`, or `Ready to promote`) comes
from the pure, unit-tested `promotionVerdict()` helper, so the UI's decision matches the backend's.

## How it maps to the CLI

The console is read-only and complements these commands (which perform the actual mutations):

```bash
exa models diff jpcp 17 18          # the version-compare the detail tabs summarise
exa models cost jpcp                # the cost tab
exa drift status                    # the drift tab / health token
exa serve traffic JPCP --production 90 --canary 10   # the traffic tab
exa pipeline promote jpcp --if-rmse-lt 5.0           # the promotion the gate evaluates
```

## Endpoints

All require the `viewer` role and are composed through the F8 BFF substrate (partial-failure safe):

| Endpoint | Returns |
|---|---|
| `GET /api/v1/mlops/registry` | `{registry: {rows, count}}` |
| `GET /api/v1/mlops/model/{name}` | `{detail: {cost, drift, traffic, promotion}}` |
| `GET /api/v1/mlops/promotion/{name}` | `{promotion: {policy, eval, approval, allowed}}` |
| `GET /api/v1/mlops/gate-reports/{name}` | `{reports: [...]}`: the model's persisted eval-gate reports, newest first |

### The eval gate on the Promotion panel (ADR 0008)

The panel shows the eval gate **as its latest persisted report found it**:
- a status pill: passed / failed / warning (not blocking) / not yet run / no gate;
- the reason;
- a per-metric table of candidate, baseline, floor, tolerated drop and verdict;
- which candidate version and baseline the report judged.

A failed `block`-mode gate denies the promotion with that reason. A `warn`-mode failure is shown
and does not block, and a configured gate that has never run is not treated as a pass.

The panel used to show the eval gate as passed whenever a *promotion policy* existed, whatever the
gate had found. It now reads `gate_reports`, the rows `run_eval_gate` writes at every
`exa pipeline promote`, `exa eval gate` and autopilot promotion. A failed latest report also raises
an alert (source `gate`): an `error` when it blocked, a `warn` in `warn` mode. A later passing report
clears it.

`name` is case-insensitive — the central `mlflow_name()` / `display_name()` mapping in `mlops.py`
resolves registry (uppercase) vs MLflow (lowercase) casing in one place.

See [`docs/reference/api.md`](../reference/api.md) for full request/response shapes and
[`docs/dashboard/architecture.md`](../dashboard/architecture.md#mlops-console-f9) for the component
diagram.

## Notes & limits

- The console reads from `platform.db` (`PLATFORM_DB`). A model only appears once it has produced at
  least one drift/cost/traffic/policy record.
- Promotion decisions and traffic changes made via the CLI are audited to `audit_events` (D4); the
  console itself performs no writes.
- Deeper tabs from the F9 spec (SLO, fairness, model-card, A/B significance) build on this substrate
  and are tracked in the dashboard-nextgen plan.
