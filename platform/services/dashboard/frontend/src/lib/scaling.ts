import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Scaling & Routing console (E4/E5 · ADR 0031/0039). Writes reuse the dashboard scaling router →
// shared examlops.autoscale.set_policy / examlops.data.gateway.set_gateway_config (pure platform.db).
// Config/policy/recorded-stats only — no live Ray runtime (actuation is out of scope).

export interface AutoscaleConfig {
  model: string
  tenant: string
  min_replicas: number
  max_replicas: number
  target_metric: string
  target_value: number
  scale_to_zero_after_s: number
  warm_pool: number
  gpu_fraction: number
  enabled: number
  updated_at: string | null
}

export interface ScaleEvent {
  id: number
  model: string
  from_replicas: number
  to_replicas: number
  reason: string | null
  metric_value: number | null
  cold_start_s: number | null
  ts: string
}

export interface ScaleSavings {
  model: string
  scale_to_zero_events: number
  saved_gpu_hours: number
  saved_cost: number
}

export interface AutoscaleView {
  model: string
  config: AutoscaleConfig | null
  events: ScaleEvent[]
  savings: ScaleSavings | null
}

export interface SetAutoscaleBody {
  model: string
  minReplicas?: number
  maxReplicas?: number
  targetMetric?: string
  targetValue?: number
  scaleToZeroAfterS?: number
  warmPool?: number
  gpuFraction?: number
  tenant?: string
}

export interface RoutingConfig {
  model: string
  tenant: string
  mode: string
  slo_latency_ms: number | null
  disaggregate: number
  prefill_pool: string | null
  decode_pool: string | null
  updated_at: string | null
}

export interface RoutingStats {
  total: number
  hits: number
  hit_rate: number
  by_decision: Record<string, number>
}

export interface RoutingView {
  model: string
  config: RoutingConfig | null
  stats: RoutingStats | null
}

export interface SetRoutingBody {
  model: string
  mode?: string
  sloLatencyMs?: number | null
  disaggregate?: boolean
  prefillPool?: string
  decodePool?: string
  tenant?: string
}

// ── autoscale ─────────────────────────────────────────────────────────────────

export const useAutoscale = (model: string) =>
  useQuery<AutoscaleView>({
    queryKey: ['scaling', 'autoscale', model],
    queryFn: () => apiFetch<AutoscaleView>(`/api/v1/scaling/autoscale?model=${encodeURIComponent(model)}`),
    enabled: !!model,
  })

export const setAutoscale = (body: SetAutoscaleBody) =>
  apiFetch('/api/v1/scaling/autoscale', { method: 'POST', body: JSON.stringify(body) })

export const useSetAutoscale = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: SetAutoscaleBody) => setAutoscale(body),
    onSuccess: (_r, body) =>
      qc.invalidateQueries({ queryKey: ['scaling', 'autoscale', body.model] }),
  })
}

// ── routing ───────────────────────────────────────────────────────────────────

export const useRouting = (model: string) =>
  useQuery<RoutingView>({
    queryKey: ['scaling', 'routing', model],
    queryFn: () => apiFetch<RoutingView>(`/api/v1/scaling/routing?model=${encodeURIComponent(model)}`),
    enabled: !!model,
  })

export const setRouting = (body: SetRoutingBody) =>
  apiFetch('/api/v1/scaling/routing', { method: 'POST', body: JSON.stringify(body) })

export const useSetRouting = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: SetRoutingBody) => setRouting(body),
    onSuccess: (_r, body) => qc.invalidateQueries({ queryKey: ['scaling', 'routing', body.model] }),
  })
}
