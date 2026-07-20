import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Authored calculation providers (ADR 0074) — the dashboard editor for the Python behind a
// project's FinOps cost/carbon (and drift/…) calculations. Every write is AST-sandboxed +
// project.manage-gated + audited at the backend; viewers only read.

export interface ProviderRow {
  domain: string
  name: string
  path: string
  ok: boolean
  error: string | null
  active: boolean
}

export interface ProviderSource {
  project: string
  domain: string
  name: string
  code: string
}

export interface SaveProviderBody {
  project: string
  domain: string
  name: string
  code: string
  activate?: boolean
}

export interface ValidateResult {
  ok: boolean
  class?: string
  error?: string
}

/** Provider domains a project can author calculations for (mirrors the CLI's known domains). */
export const PROVIDER_DOMAINS = [
  'cost',
  'carbon',
  'drift',
  'promotion',
  'llm_cost',
  'llm_cache',
  'llm_routing',
  'rag_quality',
  'placement',
] as const

/** A starter provider template shown in the "New provider" editor. Pure. */
export function providerTemplate(domain: string): string {
  const out = domain === 'carbon' ? 'co2e_g' : 'cost_usd'
  return (
    `class MyProvider(Provider):\n` +
    `    name = "my-${domain}"\n` +
    `    version = "1.0"\n\n` +
    `    def metadata(self):\n` +
    `        return ProviderMeta(methodology="describe your formula", outputs=("${out}",))\n\n` +
    `    def compute(self, inputs):\n` +
    `        # No imports needed — Provider, ProviderMeta, math are pre-injected.\n` +
    `        return {"${out}": inputs.get("gpu_hours", 0) * 1.0}\n`
  )
}

export const listProviders = (project: string): Promise<ProviderRow[]> =>
  apiFetch<ProviderRow[]>(`/api/v1/providers?project=${encodeURIComponent(project)}`)

export const readProvider = (project: string, domain: string, name: string): Promise<ProviderSource> =>
  apiFetch<ProviderSource>(
    `/api/v1/providers/${encodeURIComponent(project)}/${encodeURIComponent(domain)}/${encodeURIComponent(name)}`,
  )

export const validateProvider = (code: string): Promise<ValidateResult> =>
  apiFetch<ValidateResult>('/api/v1/providers/validate', {
    method: 'POST',
    body: JSON.stringify({ code }),
  })

export const saveProvider = (body: SaveProviderBody): Promise<ProviderRow> =>
  apiFetch<ProviderRow>('/api/v1/providers', { method: 'POST', body: JSON.stringify(body) })

export const activateProvider = (project: string, domain: string, name: string): Promise<unknown> =>
  apiFetch(
    `/api/v1/providers/${encodeURIComponent(project)}/${encodeURIComponent(domain)}/${encodeURIComponent(name)}/activate`,
    { method: 'POST' },
  )

export const deleteProvider = (project: string, domain: string, name: string): Promise<unknown> =>
  apiFetch(
    `/api/v1/providers/${encodeURIComponent(project)}/${encodeURIComponent(domain)}/${encodeURIComponent(name)}`,
    { method: 'DELETE' },
  )

export const useProviders = (project: string) =>
  useQuery<ProviderRow[]>({
    queryKey: ['providers', project],
    queryFn: () => listProviders(project),
    enabled: !!project,
  })

function invalidate(qc: ReturnType<typeof useQueryClient>, project: string) {
  qc.invalidateQueries({ queryKey: ['providers', project] })
}

export const useSaveProvider = (project: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: SaveProviderBody) => saveProvider(body),
    onSuccess: () => invalidate(qc, project),
  })
}

export const useActivateProvider = (project: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ domain, name }: { domain: string; name: string }) =>
      activateProvider(project, domain, name),
    onSuccess: () => invalidate(qc, project),
  })
}

export const useDeleteProvider = (project: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ domain, name }: { domain: string; name: string }) =>
      deleteProvider(project, domain, name),
    onSuccess: () => invalidate(qc, project),
  })
}
