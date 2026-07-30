import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Platform Ops console — reads + governed writes over the /api/v1/platform-ops router, which reuses
// the same examlops.platform_admin façade the Jupyter workbench and exa CLI use. Writes are attributed
// to the logged-in principal + audited source=dashboard; they need the platform.manage capability.

export interface CostCard {
  gpu_rate: number
  cpu_rate: number
  provider: string | null
  methodology: string | null
  cost_per_gpu_hour: number | null
}

export interface AuthoredProvider {
  domain: string
  name: string
  active: boolean
  ok: boolean
  error: string | null
}

export interface ChangeRow {
  ts: string
  source: string
  actor: string | null
  action: string
  target: string | null
  details: string | null
}

export interface Overview {
  cost_card: CostCard
  providers: AuthoredProvider[]
  changes: ChangeRow[]
}

export const usePlatformOverview = () =>
  useQuery<Overview>({
    queryKey: ['platform-ops', 'overview'],
    queryFn: () => apiFetch<Overview>('/api/v1/platform-ops/overview'),
  })

export const useSetComputeCost = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { gpu_per_hour?: number; cpu_per_hour?: number; provider?: string }) =>
      apiFetch('/api/v1/platform-ops/cost', { method: 'POST', body: JSON.stringify(body) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['platform-ops'] }),
  })
}

export const useDeployProvider = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { domain: string; name: string; code: string; project?: string }) =>
      apiFetch('/api/v1/platform-ops/provider', { method: 'POST', body: JSON.stringify(body) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['platform-ops'] }),
  })
}
