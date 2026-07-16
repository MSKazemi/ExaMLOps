import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './api'

// ── view types (mirror backend routers/nextgen.py) ───────────────────────────

export interface FederatedRun {
  run_id: string
  strategy: string
  dp_enabled: number
  secure_agg: number
  epsilon: number
  delta: number
  rounds_completed: number
  status: string
}

export interface DevicePool {
  name: string
  target: string
  accelerator: string
  capabilities: string[]
  count: number
  region: string | null
  cost_per_hour: number
  carbon_factor: number
  supports_fractions: boolean
  status: string
}

export interface PlacementDecision {
  workload: string
  accelerator_requested: string | null
  device_chosen: string | null
  pool: string | null
  target: string | null
  region: string | null
  decision: string
  fraction_honored: number
  reason: string | null
  created_at: string
}

export interface BurstEvent {
  workload: string
  from_pool: string | null
  to_pool: string | null
  residency: string | null
  allowed: number
  reason: string | null
  created_at: string
}

export interface NextGenSummary {
  federated_runs: number
  device_pools: number
  placements: number
  autoscale_configs: number
  distributed_runs: number
  feature_views: number
}

// ── pure helpers (unit-tested) ────────────────────────────────────────────────

/** Colour-blind-safe status token for a placement decision. */
export function placementTone(decision: string): 'ok' | 'warn' | 'error' | 'unknown' {
  switch (decision) {
    case 'placed':
      return 'ok'
    case 'fallback':
      return 'warn'
    case 'rejected':
      return 'error'
    default:
      return 'unknown'
  }
}

/**
 * Privacy posture label for a federated run — honest about what's actually enabled.
 * DP off → no ε/δ claimed; secure-agg off → per-site updates visible to the coordinator.
 */
export function privacyLabel(run: Pick<FederatedRun, 'dp_enabled' | 'secure_agg' | 'epsilon'>): string {
  const parts: string[] = []
  parts.push(run.dp_enabled ? `DP ε=${run.epsilon.toFixed(2)}` : 'no DP')
  parts.push(run.secure_agg ? 'secure-agg' : 'updates visible')
  return parts.join(' · ')
}

/** A burst is either permitted or blocked-by-governance — the governance signal. */
export function burstTone(allowed: number): 'ok' | 'error' {
  return allowed ? 'ok' : 'error'
}

/** Per-device-hour cost + carbon label for a pool row. */
export function poolCostLabel(pool: Pick<DevicePool, 'cost_per_hour' | 'carbon_factor'>): string {
  return `$${pool.cost_per_hour.toFixed(2)}/hr · ${Math.round(pool.carbon_factor)} gCO₂e/hr`
}

// ── data hooks ────────────────────────────────────────────────────────────────

export const useNextGenSummary = () =>
  useQuery<NextGenSummary>({
    queryKey: ['nextgen', 'summary'],
    queryFn: () => apiFetch<NextGenSummary>('/api/nextgen/summary'),
  })

export const useFederatedRuns = () =>
  useQuery<FederatedRun[]>({
    queryKey: ['nextgen', 'federated', 'runs'],
    queryFn: () => apiFetch<FederatedRun[]>('/api/nextgen/federated/runs'),
  })

export const useDevicePools = () =>
  useQuery<DevicePool[]>({
    queryKey: ['nextgen', 'hardware', 'pools'],
    queryFn: () => apiFetch<DevicePool[]>('/api/nextgen/hardware/pools'),
  })

export const usePlacementDecisions = () =>
  useQuery<PlacementDecision[]>({
    queryKey: ['nextgen', 'hardware', 'placements'],
    queryFn: () => apiFetch<PlacementDecision[]>('/api/nextgen/hardware/placements'),
  })

export const useBurstEvents = () =>
  useQuery<BurstEvent[]>({
    queryKey: ['nextgen', 'hardware', 'bursts'],
    queryFn: () => apiFetch<BurstEvent[]>('/api/nextgen/hardware/bursts'),
  })
