import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// ── view types (mirror the backend connections router) ───────────────────────
//
// Named Connections (P2, ADR 0087) are project-scoped, secret-backed handles to external
// stores (S3, URIs, dataplane). The dashboard surfaces them read-only to viewers and lets
// admins (connection.manage) create/delete/test them — the write path calls the same
// `examlops.connections` code the `exa connection create` CLI uses, so a secret entered here
// is written through `examlops.secrets` (CLI-compatible) and only `hasSecret` (a boolean) is
// ever sent back to the browser. The secret value itself never crosses the wire.

export type ConnectionKind = string

/** Fallback when the kinds endpoint is unreachable; the server list (useConnectionKinds) is
 * authoritative and additionally includes every dataplane connector's kinds (sql, kafka, …). */
export const CONNECTION_KINDS = ['s3', 'uri', 'dataplane'] as const

export interface ConnectionSummary {
  name: string
  project: string | null
  kind: ConnectionKind
  config: Record<string, unknown>
  hasSecret: boolean
  createdAt: string
  createdBy: string
}

// ── request bodies (admin-only mutations) ────────────────────────────────────

export interface CreateConnectionBody {
  name: string
  kind: ConnectionKind
  project?: string
  config?: Record<string, unknown>
  /** Optional credential — written through examlops.secrets, never returned. */
  secret?: string
}

export interface ConnectionTestResult {
  name: string
  project: string | null
  ok: boolean
  detail: string
}

// ── plain fetchers ────────────────────────────────────────────────────────────

export const listConnections = (project: string): Promise<ConnectionSummary[]> =>
  apiFetch<ConnectionSummary[]>(`/api/v1/connections?project=${encodeURIComponent(project)}`)

export const createConnection = (body: CreateConnectionBody): Promise<ConnectionSummary> =>
  apiFetch<ConnectionSummary>('/api/v1/connections', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const deleteConnection = (
  name: string,
  project: string | null,
): Promise<{ name: string; project: string | null; deleted: boolean }> => {
  const q = project ? `?project=${encodeURIComponent(project)}` : ''
  return apiFetch(`/api/v1/connections/${encodeURIComponent(name)}${q}`, { method: 'DELETE' })
}

export const testConnection = (
  name: string,
  project: string | null,
): Promise<ConnectionTestResult> => {
  const q = project ? `?project=${encodeURIComponent(project)}` : ''
  return apiFetch(`/api/v1/connections/${encodeURIComponent(name)}/test${q}`, { method: 'POST' })
}

// ── data + mutation hooks ─────────────────────────────────────────────────────

export const useConnections = (project: string) =>
  useQuery<ConnectionSummary[]>({
    queryKey: ['connections', project],
    queryFn: () => listConnections(project),
    enabled: !!project,
  })

export const useCreateConnection = (project: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: CreateConnectionBody) => createConnection(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['connections', project] })
      qc.invalidateQueries({ queryKey: ['projects', 'detail', project] })
    },
  })
}

export const useDeleteConnection = (project: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name: string) => deleteConnection(name, project || null),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['connections', project] })
      qc.invalidateQueries({ queryKey: ['projects', 'detail', project] })
    },
  })
}

export const useTestConnection = (project: string) =>
  useMutation({
    mutationFn: (name: string) => testConnection(name, project || null),
  })

/** Every connection kind the dataplane connector registry accepts (base kinds ∪ registry
 * kinds — sql, kafka, rest, zenodo, fs, …). Falls back to CONNECTION_KINDS if unreachable. */
export function useConnectionKinds(): string[] {
  const q = useQuery({
    queryKey: ['connections', 'kinds'],
    queryFn: () => apiFetch<{ kinds: string[] }>('/api/v1/connections/kinds'),
    staleTime: 5 * 60_000,
  })
  return q.data?.kinds ?? [...CONNECTION_KINDS]
}
