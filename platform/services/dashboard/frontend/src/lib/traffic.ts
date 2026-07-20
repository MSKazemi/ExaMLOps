import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Serving traffic split (admin) — mirrors `exa serve traffic`. Weights per alias must sum to 100.
// Reads are viewer-visible; the PUT is admin-gated + audited at the BFF.

export interface TrafficRules {
  model: string
  rules: Record<string, number>
  updated_at?: string
  updated_by?: string
}

/** Canonical serving aliases the UI offers weights for (mirrors RAY_PRELOAD_ALIASES). */
export const TRAFFIC_ALIASES = ['Production', 'Canary', 'Staging'] as const

/** Sum of a weight map — the UI blocks Save unless this is exactly 100. Pure. */
export function weightSum(rules: Record<string, number>): number {
  return Object.values(rules).reduce((a, b) => a + (Number(b) || 0), 0)
}

export const getModelTraffic = (model: string): Promise<TrafficRules | null> =>
  apiFetch<TrafficRules | null>(`/api/platform-data/traffic-rules/${encodeURIComponent(model)}`)

export const setModelTraffic = (
  model: string,
  rules: Record<string, number>,
): Promise<TrafficRules> =>
  apiFetch<TrafficRules>(`/api/platform-data/traffic-rules/${encodeURIComponent(model)}`, {
    method: 'PUT',
    body: JSON.stringify({ rules }),
  })

export const useModelTraffic = (model: string | null) =>
  useQuery<TrafficRules | null>({
    queryKey: ['traffic-rules', model],
    queryFn: () => getModelTraffic(model as string),
    enabled: !!model,
  })

export const useSetModelTraffic = (model: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (rules: Record<string, number>) => setModelTraffic(model, rules),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['traffic-rules', model] })
      qc.invalidateQueries({ queryKey: ['traffic-rules'] })
    },
  })
}
