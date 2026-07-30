import { useMutation } from '@tanstack/react-query'
import { apiFetch } from './api'

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
    mutationFn: (vars: { question: string; context: CopilotContext; session?: string }) =>
      apiFetch<CopilotResponse>('/api/v1/copilot/ask', {
        method: 'POST',
        body: JSON.stringify({
          question: vars.question,
          context: vars.context,
          session: vars.session ?? 'dashboard-copilot',
        }),
      }),
  })
}
