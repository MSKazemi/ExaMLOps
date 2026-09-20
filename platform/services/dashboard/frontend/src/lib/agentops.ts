import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './api'

// Agent runs console (ADR 0021). Read-only: every call reads the dashboard agentops router →
// the same agent_sessions / agent_tool_calls rows `exa agentops sessions|replay|tools` read. Tool
// arguments are never returned, only the redacted digest the recorder stored.

export interface AgentSession {
  session_id: string
  tenant: string
  agent: string | null
  model: string | null
  steps: number
  tool_calls: number
  errors: number
  input_tokens: number
  output_tokens: number
  cost_usd: number
  status: string
  anomalies: string[]
  started_at: string
  ended_at: string | null
}

export interface AgentStep {
  step: number
  tool: string
  args_digest: string | null
  ok: number
  error: string | null
  latency_ms: number | null
  ts: string
}

export interface AgentReplay {
  session: AgentSession
  steps: AgentStep[]
}

export interface AgentToolStat {
  tool: string
  calls: number
  errors: number
  success_rate: number
  avg_latency_ms: number | null
}

export interface BreakerEvent {
  ts: string
  event: 'warning' | 'tripped'
  target: string | null
  details: { detail?: string; tool?: string | null }
}

export const useAgentSessions = (status?: string) =>
  useQuery<AgentSession[]>({
    queryKey: ['agentops', 'sessions', status ?? 'all'],
    queryFn: () =>
      apiFetch<AgentSession[]>(`/api/agentops/sessions${status ? `?status=${encodeURIComponent(status)}` : ''}`),
  })

export const useAgentReplay = (sessionId: string | null) =>
  useQuery<AgentReplay>({
    queryKey: ['agentops', 'replay', sessionId],
    queryFn: () => apiFetch<AgentReplay>(`/api/agentops/sessions/${encodeURIComponent(sessionId ?? '')}`),
    enabled: !!sessionId,
  })

export const useAgentTools = () =>
  useQuery<AgentToolStat[]>({
    queryKey: ['agentops', 'tools'],
    queryFn: () => apiFetch<AgentToolStat[]>('/api/agentops/tools'),
  })

export const useBreakerEvents = () =>
  useQuery<BreakerEvent[]>({
    queryKey: ['agentops', 'breaker'],
    queryFn: () => apiFetch<BreakerEvent[]>('/api/agentops/breaker'),
  })
