import { useMutation } from '@tanstack/react-query'
import { apiFetch } from './api'
import { ApiError } from './errors'

// Embedded copilot client (F11 / ADR 0065). Talks to the BFF `/api/v1/copilot/ask`, which proxies the
// existing Skipper agent. Proposals are advisory — the copilot never executes anything (R5).

export interface CopilotContext {
  page: string
  entity?: Record<string, unknown>
  filters?: Record<string, unknown>
}

export interface ActionProposal {
  command: string
  requiresApproval: boolean
}

export interface AgentTraceStep {
  kind: string
  name?: string | null
  detail: string
}

export interface CopilotResponse {
  answer: string
  hitl_required: boolean
  proposals: ActionProposal[]
  trace: AgentTraceStep[]
  error_code?: 'agent_auth' | 'agent_response' | 'agent_timeout' | 'agent_unavailable' | 'agent_error'
  _partial?: string[]
}

export interface CopilotTurn {
  role: 'user' | 'assistant'
  text: string
  response?: CopilotResponse
}

/** Lifecycle-group URL prefixes (ADR 0097 §1) — stripped so entity grounding sees the console segment. */
const GROUP_PREFIXES = new Set(['build', 'serve', 'operate', 'govern', 'platform'])

/**
 * Derive grounding context from the current route (F11 R2). A leading lifecycle group is ignored, so
 * both `/models/jpcp` and `/build/models/jpcp` → entity {type:'models', id:'jpcp'}.
 */
export function buildContext(pathname: string, filters?: Record<string, unknown>): CopilotContext {
  let seg = pathname.split('/').filter(Boolean)
  if (seg.length > 0 && GROUP_PREFIXES.has(seg[0])) seg = seg.slice(1)
  const entity = seg.length >= 2 ? { type: seg[0], id: seg[1] } : undefined
  return { page: pathname || '/', entity, filters }
}

/** True when the copilot ran but the agent backend was unavailable (degraded envelope). */
export function isDegraded(res: CopilotResponse): boolean {
  return Array.isArray(res._partial) && res._partial.includes('agent')
}

/** Short human label for a proposal's execution gate (R5). */
export function proposalGateLabel(p: ActionProposal): string {
  return p.requiresApproval ? 'Needs approval' : 'Read-only'
}

export function useCopilotAsk() {
  return useMutation({
    mutationFn: (vars: { question: string; context: CopilotContext }) =>
      apiFetch<CopilotResponse>('/api/v1/copilot/ask', {
        method: 'POST',
        body: JSON.stringify({
          question: vars.question,
          context: vars.context,
        }),
      }),
  })
}

/**
 * What to tell the user when the copilot request itself failed (the dashboard API, not the model:
 * a model failure arrives as a normal 200 answer that already says why).
 *
 * The panel used to show one sentence for every failure, so an expired session, a missing
 * permission and a down API all looked the same. Each is a different next step.
 */
export function describeCopilotError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return 'Your session has expired. Sign in again, then retry.'
    if (err.status === 403) return 'Your role is not permitted to use the copilot.'
    if (err.status === 429) return 'Too many copilot requests. Wait a moment, then try again.'
    if (err.status >= 500) {
      return `The dashboard API failed (HTTP ${err.status}). Try again; if it keeps failing, check the dashboard logs.`
    }
    return `The copilot request was rejected (HTTP ${err.status}): ${err.message}`
  }
  if (err instanceof TypeError) {
    return 'Cannot reach the dashboard API. Check your network connection, then try again.'
  }
  return 'The copilot request failed unexpectedly. Please try again.'
}
