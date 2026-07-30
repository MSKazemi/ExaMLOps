import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Feature store (A3). Writes reuse the dashboard feature-store router → shared
// examlops.feature_store.apply_view (pure platform.db). One view = one train/serve definition.

export interface FeatureView {
  name: string
  entity: string
  features: string[]
  source: string | null
  ttl_seconds: number
  dataset_revision: string | null
  updated_at: string | null
}

export interface ApplyViewBody {
  name: string
  entity: string
  features: string[]
  source?: string
  ttlSeconds?: number
  datasetRevision?: string
}

export const useFeatureViews = () =>
  useQuery<FeatureView[]>({
    queryKey: ['feature-views'],
    queryFn: () => apiFetch<FeatureView[]>('/api/feature-store/views'),
  })

export const applyFeatureView = (body: ApplyViewBody) =>
  apiFetch('/api/feature-store/views', { method: 'POST', body: JSON.stringify(body) })

export const useApplyFeatureView = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: ApplyViewBody) => applyFeatureView(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['feature-views'] }),
  })
}
