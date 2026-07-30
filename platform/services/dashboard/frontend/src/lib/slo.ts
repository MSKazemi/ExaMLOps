import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Model-quality SLOs (C6 / ADR 0023). Writes reuse the dashboard SLO router → shared
// examlops.slo.apply_spec (pure platform.db). Live status is best-effort (from slo_samples).

export interface SloStatus {
  sli: number
  budgetRemaining: number
  burnRate: number | null
  ok: boolean
  n: number
}

export interface SloSpec {
  model: string
  tenant: string
  name: string
  sli_source: string
  sli_query: string | null
  target: number
  window: string
  higher_is_better: boolean
  version: number
  gate_promotion: boolean
  updated_at: string | null
  status: SloStatus | null
}

export interface SetSloBody {
  model: string
  name: string
  target: number
  sliSource?: string
  sliQuery?: string
  window?: string
  higherIsBetter?: boolean
  gatePromotion?: boolean
}

export const useSlos = () =>
  useQuery<SloSpec[]>({ queryKey: ['slo'], queryFn: () => apiFetch<SloSpec[]>('/api/slo') })

export const setSlo = (body: SetSloBody) =>
  apiFetch('/api/slo', { method: 'POST', body: JSON.stringify(body) })

export const useSetSlo = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: SetSloBody) => setSlo(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['slo'] }),
  })
}
