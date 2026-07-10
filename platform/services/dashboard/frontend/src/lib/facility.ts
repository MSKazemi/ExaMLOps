import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './api'

// ── view types (mirror backend facility.py / F6 interfaces) ──────────────────

export interface PartitionUtil {
  name: string
  running: number
  queued: number
  gpusAllocated: number
}

export interface FacilityOverview {
  nodesAllocated: number
  gpusAllocated: number
  jobsRunning: number
  queueDepth: number
  clusters: string[]
  partitions: PartitionUtil[]
}

export interface QueuedJob {
  id: string
  cluster: string
  model: string
  dataset: string
  state: string
  waitSec: number
  nodes: number | null
  gpus: number | null
  submitTime: string | null
}

export interface OverviewResponse {
  facility: FacilityOverview
  _partial?: string[]
}

export interface QueueResponse {
  queue: { jobs: QueuedJob[]; count: number }
  _partial?: string[]
}

// ── pure helpers (unit-tested) ────────────────────────────────────────────────

/** Human wait-time label from seconds — the queue's ordering signal (F6 R2). */
export function waitLabel(sec: number): string {
  if (sec < 60) return `${Math.round(sec)}s`
  if (sec < 3600) return `${Math.round(sec / 60)}m`
  const h = Math.floor(sec / 3600)
  const m = Math.round((sec % 3600) / 60)
  return m ? `${h}h ${m}m` : `${h}h`
}

/**
 * Utilization ratio (queued vs running) → colour-blind-safe token for a partition (F6 R1).
 * A backed-up queue (more waiting than running) is the pressure signal.
 */
export function partitionTone(p: PartitionUtil): 'ok' | 'warn' | 'unknown' {
  if (p.running === 0 && p.queued === 0) return 'unknown'
  if (p.queued > p.running) return 'warn'
  return 'ok'
}

// ── data hooks ────────────────────────────────────────────────────────────────

const clusterQuery = (cluster: string | null) => (cluster ? `?cluster=${encodeURIComponent(cluster)}` : '')

export const useFacilityOverview = (cluster: string | null = null) =>
  useQuery<OverviewResponse>({
    queryKey: ['facility', 'overview', cluster],
    queryFn: () => apiFetch<OverviewResponse>(`/api/v1/facility/overview${clusterQuery(cluster)}`),
  })

export const useFacilityQueue = (cluster: string | null = null) =>
  useQuery<QueueResponse>({
    queryKey: ['facility', 'queue', cluster],
    queryFn: () => apiFetch<QueueResponse>(`/api/v1/facility/queue${clusterQuery(cluster)}`),
  })
