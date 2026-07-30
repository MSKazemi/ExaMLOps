import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Fairness config (C8). Writes reuse the dashboard fairness router → shared
// examlops.data.governance.set_fairness_config (pure platform.db).

export interface FairnessConfig {
  model: string
  tenant: string
  slice_attrs: string[]
  threshold: number
  min_samples: number
  gate_promotion: boolean
  enabled: boolean
  updated_at: string | null
}

export interface SetFairnessBody {
  model: string
  sliceAttrs: string[]
  threshold?: number
  minSamples?: number
  gatePromotion?: boolean
  enabled?: boolean
}

export const useFairnessConfigs = () =>
  useQuery<FairnessConfig[]>({ queryKey: ['fairness'], queryFn: () => apiFetch<FairnessConfig[]>('/api/fairness') })

export const setFairness = (body: SetFairnessBody) =>
  apiFetch('/api/fairness', { method: 'POST', body: JSON.stringify(body) })

export const useSetFairness = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: SetFairnessBody) => setFairness(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['fairness'] }),
  })
}
