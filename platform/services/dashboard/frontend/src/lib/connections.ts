import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './api'

// ── view types (mirror the backend connections router) ───────────────────────
//
// Named Connections (P2, ADR 0087) are project-scoped, secret-backed handles to external
// stores (S3, URIs, dataplane). The dashboard surfaces them READ-ONLY: creation lives in the
// CLI (`exa connection create`) because it involves secrets. `hasSecret` is a boolean only —
// the secret value itself is never sent to the browser.

export type ConnectionKind = 's3' | 'uri' | 'dataplane'

export interface ConnectionSummary {
  name: string
  project: string | null
  kind: ConnectionKind
  config: Record<string, unknown>
  hasSecret: boolean
  createdAt: string
  createdBy: string
}

// ── plain fetcher ─────────────────────────────────────────────────────────────

export const listConnections = (project: string): Promise<ConnectionSummary[]> =>
  apiFetch<ConnectionSummary[]>(`/api/v1/connections?project=${encodeURIComponent(project)}`)

// ── data hook ─────────────────────────────────────────────────────────────────

export const useConnections = (project: string) =>
  useQuery<ConnectionSummary[]>({
    queryKey: ['connections', project],
    queryFn: () => listConnections(project),
    enabled: !!project,
  })
