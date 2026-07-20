import { useMutation, useQueryClient } from '@tanstack/react-query'
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
