import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'
import { resolveFlag, FLAGS, type FlagName } from './flags'

// Server-evaluated feature flags (F25 / ADR 0070).
//
// The BFF evaluates flags with the caller's context (tenant/role/percentage) and returns *decisions*.
// `useFlag` prefers the server decision and falls back to the client-side default (F23 `flags.ts`) so
// the UI still works before the flags payload arrives or if the endpoint is unavailable.

export interface FlagDecisions {
  flags: Record<string, boolean>
}

export interface FlagAdminRow {
  name: string
  description: string
  default: boolean
  override: boolean | null
  effective: boolean
  targeting: { tenants: string[]; roles: string[]; percentage: number | null }
  tags: string[]
}

export interface FlagAdminView {
  flags: FlagAdminRow[]
  count: number
}

// ── decisions (R2) ──────────────────────────────────────────────────────────

export const useFlagDecisions = () =>
  useQuery<FlagDecisions>({
    queryKey: ['flags', 'decisions'],
    queryFn: () => apiFetch<FlagDecisions>('/api/v1/flags'),
    staleTime: 30_000,
  })

/**
 * `useFlag(name)` — the server decision when available, else the client-side default (F25 R2).
 * Non-reactive callers can still use `resolveFlag` from `flags.ts`.
 */
export function useFlag(name: FlagName | string): boolean {
  const { data } = useFlagDecisions()
  const server = data?.flags?.[name]
  return server !== undefined ? server : flagFallback(name)
}

/** Client-side fallback for a flag: its default if the client knows it, else off. Pure. */
export function flagFallback(name: string): boolean {
  return name in FLAGS ? resolveFlag(name as FlagName) : false
}

// ── admin (R4) ───────────────────────────────────────────────────────────────

export const useFlagAdmin = () =>
  useQuery<FlagAdminView>({
    queryKey: ['flags', 'admin'],
    queryFn: () => apiFetch<FlagAdminView>('/api/v1/flags/admin'),
  })

export const useSetFlag = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      apiFetch(`/api/v1/flags/${encodeURIComponent(name)}`, {
        method: 'POST',
        body: JSON.stringify({ enabled }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['flags', 'admin'] })
      qc.invalidateQueries({ queryKey: ['flags', 'decisions'] })
    },
  })
}
