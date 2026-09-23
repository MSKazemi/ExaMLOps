# LLMOps Console

The **LLMOps** page surfaces the platform's LLM-serving substrate: the LLM endpoint registry and
continuous-eval scores. Surfaces whose backends aren't wired yet degrade gracefully — they appear as
"not yet available" rather than errors.

Open it from the sidebar (**LLMOps**) or navigate to `/serve/llmops` (the ADR 0097 nav
reorganization moved it here; the old `/llmops` path still works via a one-release-only redirect
shim in `ROUTE_REDIRECTS`, do not depend on it).

Two sibling consoles have since shipped alongside this one and aren't yet cross-linked from here:
**Gateway** (`/serve/gateway`) for the LLM gateway's virtual keys/routes/health, and **Prompts**
(`/build/prompts`) for the versioned prompt registry (ADR 0009). This page's own scope stays the
endpoint registry + continuous-eval surfaces described below.

- **Feature:** F10 · **Design:** ADR 0064 (`design/adr/0064-dashboard-llmops-console.md`) ·
  **Spec:** `design/vision/specs/F10-llmops-console.md`
- **Backend:** `platform/services/dashboard/backend/llmops.py` + `routers/llmops.py`
- **Frontend:** `platform/services/dashboard/frontend/src/lib/llmops.ts` + `pages/Llmops.tsx`

## What you see

### Endpoint registry (R2)

Each LLM endpoint from `llm_endpoints`: model name, serving engine (e.g. vLLM), Hugging Face model id,
tensor-parallel size, dtype, and enabled/disabled status.

### Continuous eval (R1 / C2)

Per model, the **latest** eval run's suite/status plus its metric results — each metric's value, its
baseline, and a pass/fail badge — with an overall `passRate` pill (green = all passed, amber = some,
red = none).

## Endpoint

```
GET /api/v1/llmops/overview     # viewer role; BFF-composed, partial-failure safe
```

Returns `{endpoints, evals}`. See [`docs/reference/api.md`](../reference/api.md) for the full shape and
[`docs/dashboard/architecture.md`](../dashboard/architecture.md#llmops-console-f10) for the diagram.

## Notes & limits

- Reads from `platform.db` (`PLATFORM_DB`); missing tables degrade to empty sections, never an error.
- This slice ships the endpoint registry + eval scores. Some of the richer F10 surfaces have since
  shipped as their own pages rather than growing inside this one: a versioned **prompt registry**
  (`/build/prompts`, ADR 0009 — not the originally-planned "prompt studio" diff/rollback editor,
  which remains unbuilt) and a real LLM **gateway** (`/serve/gateway`, ADR 0151-0156 — not a
  LiteLLM proxy, see `docs/guides/model-gateway.md`, with routing/resilience/virtual-key cost
  attribution; rate-limiting beyond per-key dollar budgets is not yet built, see
  `.claude/plans/BACKLOG.md` BL-107). **semantic-cache** metrics (R3), **RAG-ops** pipeline +
  retrieval quality (R4), and **vector-DB/embedding-lifecycle** views (R5) remain unbuilt and are
  tracked in the dashboard-nextgen plan.
