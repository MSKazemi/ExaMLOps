# Embedded Copilot

The dashboard has a context-aware **copilot** in the shell. It answers grounded questions about the
platform and can **suggest** `exa` commands — but it never runs anything. It is a thin proxy to the
**existing** Skipper agent (the same one `exa ask` uses), so there is no new model and no paid API.

- **Feature:** F11 · **Design:** ADR 0065 (`design/adr/0065-dashboard-embedded-copilot.md`) ·
  **Spec:** `design/vision/specs/F11-embedded-copilot.md`
- **Backend:** `platform/services/dashboard/backend/copilot.py` + `routers/copilot.py`
- **Frontend:** `platform/services/dashboard/frontend/src/lib/copilot.ts` + `components/CopilotPanel.tsx`

## Using it

Open the **Copilot** launcher (bottom-right, on every page). Ask in plain English — e.g. "why is jpcp
drifting?", "what's my GPU spend this month?", "how do I promote a model?". The current page and entity
are sent as grounding context, so answers are specific to what you're looking at.

## Propose-only — the copilot never executes (R5)

When the copilot suggests an action it renders the `exa` command as a **copy-only card** with a gate
badge:

- **Read-only** — a safe query command (e.g. `exa drift status`); copy and run it as-is.
- **Needs approval** — a mutating command (retrain, promote, approve, traffic split, …). Copy it, then
  run it through the normal authorized + approval-gated + audited flow. There is **no "run" button** in
  the copilot — this is a hard rule (ADR 0065).

The classification is server-side (`extract_proposals`), so a mutating command is always flagged even if
the model's prose doesn't say so.

## Grounding & safety

| Concern | Mechanism |
|---|---|
| Page context (R2) | `buildContext(pathname)` sends `{page, entity, filters}`; the backend injects it into the system prompt |
| Prompt injection (R6) | page context is framed as **UNTRUSTED data, never instructions** in the system prompt |
| Output sanitization (R1) | answers render through `sanitizeMarkdown` (F16) — scripts/handlers/js: URIs stripped |
| Transparency (R6) | the agent's tool-call trace is shown in a collapsible panel |
| Audit (D4) | every query writes a `copilot_query` event to `audit_events` (`source = dashboard-copilot`) |
| Availability | if the agent is down the copilot returns a graceful fallback (`_partial: ["agent"]`), never a 500 |

## Endpoint

| Endpoint | Role | Purpose |
|---|---|---|
| `POST /api/v1/copilot/ask` | viewer | `{question, context, session}` → `{answer, hitl_required, proposals[], trace[]}` |

The endpoint proxies `AGENT_URL` (default `http://localhost:18004`); set it (or `exa config set agent
<url>`) to point at your Skipper bridge. `AGENT_API_KEY` is forwarded as a bearer token if set.

## Notes & limits

This slice ships the grounded Q&A, propose-only actions, trace, and audit. Deferred (tracked in the
plan): **streaming** over the F8 WebSocket (currently single-shot), **NL→in-app-view** actuation (e.g.
filtering the facility queue directly), and a **one-click confirm** that routes a proposal into the
approval gate.

See [`docs/dashboard/architecture.md`](../dashboard/architecture.md#embedded-copilot-f11) for the design
diagram.
