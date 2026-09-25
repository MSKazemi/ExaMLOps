import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// LLM gateway virtual keys (B2). Writes reuse the dashboard gateway router → shared
// examlops.gateway path. The raw key is returned only by `issueKey` (shown once); list/reads
// expose the hash + scope only, never the raw key.

export interface VirtualKey {
  key_hash: string
  tenant: string
  project: string
  models: string[]
  budget_usd: number | null
  spent_usd: number
  created_by: string | null
  created_at: string | null
  revoked: boolean
}

export interface IssueKeyBody {
  tenant?: string
  project?: string
  models?: string[]
  budgetUsd?: number
}

export interface IssuedKey {
  key: string
  tenant: string
  project: string
  models: string[]
  budgetUsd: number | null
}

export const useVirtualKeys = () =>
  useQuery<VirtualKey[]>({
    queryKey: ['gateway', 'keys'],
    queryFn: () => apiFetch<VirtualKey[]>('/api/gateway/keys'),
  })

export const issueKey = (body: IssueKeyBody): Promise<IssuedKey> =>
  apiFetch<IssuedKey>('/api/gateway/keys', { method: 'POST', body: JSON.stringify(body) })

export const revokeKey = (keyHash: string): Promise<{ keyHash: string; revoked: boolean }> =>
  apiFetch(`/api/gateway/keys/${encodeURIComponent(keyHash)}/revoke`, { method: 'POST' })

function invalidate(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ['gateway'] })
}

export const useIssueKey = () => {
  const qc = useQueryClient()
  return useMutation({ mutationFn: (body: IssueKeyBody) => issueKey(body), onSuccess: () => invalidate(qc) })
}

export const useRevokeKey = () => {
  const qc = useQueryClient()
  return useMutation({ mutationFn: (keyHash: string) => revokeKey(keyHash), onSuccess: () => invalidate(qc) })
}

// Live status (P6) — proxies the deployed llm-gateway's own unauthenticated `/ready`; never the
// admin-token-gated `/admin/health`/`/admin/config` (see the backend router's own docstring for why).

export interface GatewayStatus {
  reachable: boolean
  ready: boolean
  healthyNow: boolean
  routes: Record<string, { healthy: boolean; required: boolean; deployments: number }>
  warnings: string[]
}

export const useGatewayStatus = () =>
  useQuery<GatewayStatus>({
    queryKey: ['gateway', 'status'],
    queryFn: () => apiFetch<GatewayStatus>('/api/gateway/status'),
    refetchInterval: 30_000,
  })

// Test chat (P6, admin) — one real message through the deployed gateway with an operator-supplied
// virtual key, mirroring `exa gateway chat --key`. Never stores the key.

export interface TestChatBody {
  message: string
  route?: string
  key?: string
}

export interface TestChatResult {
  ok: boolean
  status: number | null
  latencyMs: number
  reply?: string
  error?: string
  code?: string
}

export const testChat = (body: TestChatBody): Promise<TestChatResult> =>
  apiFetch<TestChatResult>('/api/gateway/test-chat', { method: 'POST', body: JSON.stringify(body) })

export const useTestChat = () => useMutation({ mutationFn: (body: TestChatBody) => testChat(body) })
