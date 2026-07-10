# FinOps & Green-AI

The **FinOps** page surfaces the platform's cost and carbon accounting: GPU-hour spend by model,
budget-vs-actual, estimated carbon emissions, and unit economics. It renders the shipped phase 23/24
cost/carbon backend and is honest about estimation uncertainty.

Open it from the sidebar (**FinOps**, the `$` icon) or navigate to `/finops`.

- **Feature:** F13 · **Design:** [ADR 0066](../../design/adr/0066-dashboard-finops-green-ai-surface.md) ·
  **Spec:** `design/vision/specs/F13-finops-green-ai-surface.md`
- **Backend:** `platform/services/dashboard/backend/finops.py` + `routers/finops.py`
- **Frontend:** `platform/services/dashboard/frontend/src/lib/finops.ts` + `pages/Finops.tsx`

## What you see

- **KPI tiles** (reusing the F4 `<KpiTile>`): total spend, GPU-hours, estimated carbon (with its ±
  uncertainty band), and cost-per-training-run.
- **Cost by model** — a table of GPU-hours, run count, and USD per model (rolled up from `model_costs`).
- **Budgets** — per-project budget usage (GPU-hour % and cost %) with an **Over budget** pill when
  actuals exceed the configured budget.
- **Carbon methodology footnote** — the estimation method + uncertainty, always shown so figures are
  never read as exact.

## Data source & CLI

The page reads the same tables the `exa finops` CLI writes:

```bash
exa finops budget set eu-hpc --gpu-hours 1000 --cost 5000   # project_budgets
exa finops budget status                                    # budget-vs-actual
exa finops carbon estimate --gpu-hours 12                   # carbon estimate
exa finops carbon record JPCP --gpu-hours 12                # carbon_records
exa models cost JPCP --record                               # model_costs (GPU-hours → USD)
```

## Endpoint

```
GET /api/v1/finops/overview     # viewer role; BFF-composed, partial-failure safe
```

Returns `{cost, budget, carbon, unitEconomics}`. See
[`docs/reference/api.md`](../reference/api.md) for the full shape and
[`docs/dashboard/architecture.md`](../dashboard/architecture.md#finops--green-ai-f13) for the diagram.

## Honest estimation (R3)

Carbon figures are estimates: `Energy = GPU-hours × TDP × PUE`, `CO₂e = Energy × grid intensity`. Grid
intensity and TDP vary, so every carbon payload carries an `uncertainty` fraction (±30%) and a
`methodology` string — the UI shows a "kg ±%" band, never false precision.

## Notes & limits

- Reads from `platform.db` (`PLATFORM_DB`); missing tables degrade to zeros, never an error.
- `model_costs` has no project column yet, so budget "consumed" is the facility total charged against
  each budget; **per-project cost attribution** lands with the F15 tenant columns.
- This slice ships cost rollup + budgets + carbon + unit economics. The richer F13 surfaces — a
  cost-allocation **Sankey** + drill-through (R1), **burn-rate forecast** + overspend alert to F12
  (R2), SCI / energy-mix trend + facility PUE (R3), **gated waste reclaim** (R5), and **chargeback
  export** (R6) — build on this and are tracked in the dashboard-nextgen plan.
