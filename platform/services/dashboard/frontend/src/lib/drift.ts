import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Drift write actions (admin) — mirror `exa drift baseline|reset|auto-retrain`. Each mutation
// invalidates the drift queries so the tables reflect the change immediately. Reads stay in Drift.tsx.

export interface SetBaselineResult {
  model: string
  baseline: Record<string, number>
}

export interface ResetResult {
  model: string
  cleared: number
}

export interface AutoRetrainBody {
  enabled: boolean
  dataset?: string
  minZ?: number
  cooldown?: number
}

export const setDriftBaseline = (model: string): Promise<SetBaselineResult> =>
  apiFetch<SetBaselineResult>(`/api/drift/baseline/${encodeURIComponent(model)}`, { method: 'POST' })

export const resetDrift = (model: string): Promise<ResetResult> =>
  apiFetch<ResetResult>(`/api/drift/reset/${encodeURIComponent(model)}`, { method: 'POST' })

export const setAutoRetrain = (model: string, body: AutoRetrainBody): Promise<AutoRetrainBody> =>
  apiFetch<AutoRetrainBody>(`/api/drift/auto-retrain/${encodeURIComponent(model)}`, {
    method: 'POST',
    body: JSON.stringify(body),
  })

// Input-drift edit parity (BL-014) — mirror `exa drift input baseline|reset`.
export const setInputBaseline = (model: string): Promise<SetBaselineResult> =>
  apiFetch<SetBaselineResult>(`/api/drift/input-baseline/${encodeURIComponent(model)}`, {
    method: 'POST',
  })

export const resetInputDrift = (model: string): Promise<ResetResult> =>
  apiFetch<ResetResult>(`/api/drift/input-reset/${encodeURIComponent(model)}`, { method: 'POST' })

/** Invalidate every drift query so all three tabs refetch. */
function invalidateDrift(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ['drift-status'] })
  qc.invalidateQueries({ queryKey: ['drift-input-status'] })
  qc.invalidateQueries({ queryKey: ['drift-auto-retrain'] })
}

export const useSetDriftBaseline = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (model: string) => setDriftBaseline(model),
    onSuccess: () => invalidateDrift(qc),
  })
}

export const useResetDrift = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (model: string) => resetDrift(model),
    onSuccess: () => invalidateDrift(qc),
  })
}

export const useSetAutoRetrain = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ model, body }: { model: string; body: AutoRetrainBody }) =>
      setAutoRetrain(model, body),
    onSuccess: () => invalidateDrift(qc),
  })
}

export const useSetInputBaseline = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (model: string) => setInputBaseline(model),
    onSuccess: () => invalidateDrift(qc),
  })
}

export const useResetInputDrift = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (model: string) => resetInputDrift(model),
    onSuccess: () => invalidateDrift(qc),
  })
}

// Unified drift events (ADR 0022) — `exa drift events`: concept, label-free estimate and
// data-quality detections written by `exa drift run-advanced` (or the per-command detectors).
export interface DriftEvent {
  id: number
  ts: string
  model: string
  drift_kind: string
  severity: string
  score: number | null
  metric: string | null
  detail: Record<string, unknown> | null
}

export const useDriftEvents = (kind?: string) =>
  useQuery<DriftEvent[]>({
    queryKey: ['drift-events', kind ?? 'all'],
    queryFn: () => apiFetch<DriftEvent[]>(`/api/drift/events${kind ? `?kind=${encodeURIComponent(kind)}` : ''}`),
  })
