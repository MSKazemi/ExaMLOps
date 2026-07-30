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

// ── A/B testing + shadow deployments ──────────────────────────────────────────
// Traffic console — mirrors `exa serve ab` / `exa serve shadow` over the dashboard traffic router →
// shared examlops.data code paths (pure platform.db; no Ray). Reads viewer-visible; writes admin +
// `traffic.manage` and audited `source=dashboard` at the BFF.

export interface AbTest {
  id: number
  model: string
  name: string | null
  variant_a: string
  variant_b: string
  split_pct: number
  status: string
  started_at: string | null
  ended_at: string | null
  created_by: string | null
}

export interface AbAnalysis {
  model: string
  test_id: number
  variant_a: string
  variant_b: string
  verdict?: string
  significant: boolean
  winner: string | null
  n_a: number
  n_b: number
  p_value?: number
  t_stat?: number
  mean_a?: number
  mean_b?: number
  min_sample?: number
}

export interface AbView {
  tests: AbTest[]
  analysis: AbAnalysis | null
}

export interface AbStartBody {
  model: string
  variant_a?: string
  variant_b?: string
  split?: number
  name?: string
}

export interface ShadowConfig {
  model: string
  shadow_alias: string
  enabled: number
  updated_at: string | null
  updated_by: string | null
}

export interface ShadowResult {
  id: number
  ts: string
  model: string
  production_pred: number | null
  shadow_pred: number | null
  diff_pct: number | null
  job_id: string | null
}

export interface ShadowView {
  config: ShadowConfig[]
  results: ShadowResult[]
}

export interface SetShadowBody {
  model: string
  enabled: boolean
  shadow_alias?: string
}

export const getAb = (model: string): Promise<AbView> =>
  apiFetch<AbView>(`/api/v1/traffic/ab?model=${encodeURIComponent(model)}`)

export const useAb = (model: string | null) =>
  useQuery<AbView>({
    queryKey: ['traffic-ab', model],
    queryFn: () => getAb(model as string),
    enabled: !!model,
  })

export const startAbTest = (body: AbStartBody): Promise<AbTest> =>
  apiFetch<AbTest>('/api/v1/traffic/ab/start', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const stopAbTest = (model: string): Promise<{ model: string; stopped: boolean }> =>
  apiFetch<{ model: string; stopped: boolean }>('/api/v1/traffic/ab/stop', {
    method: 'POST',
    body: JSON.stringify({ model }),
  })

export const useStartAbTest = (model: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: AbStartBody) => startAbTest(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['traffic-ab', model] }),
  })
}

export const useStopAbTest = (model: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => stopAbTest(model),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['traffic-ab', model] }),
  })
}

export const getShadow = (model: string): Promise<ShadowView> =>
  apiFetch<ShadowView>(`/api/v1/traffic/shadow?model=${encodeURIComponent(model)}`)

export const useShadow = (model: string | null) =>
  useQuery<ShadowView>({
    queryKey: ['traffic-shadow', model],
    queryFn: () => getShadow(model as string),
    enabled: !!model,
  })

export const setShadow = (body: SetShadowBody): Promise<ShadowConfig> =>
  apiFetch<ShadowConfig>('/api/v1/traffic/shadow', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const useSetShadow = (model: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: SetShadowBody) => setShadow(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['traffic-shadow', model] }),
  })
}
