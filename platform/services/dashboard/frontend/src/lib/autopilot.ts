import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Self-driving autopilot kill-switch (ADR 0085). Writes reuse the dashboard autopilot router →
// shared examlops.data.autopilot.set_autopilot_config (pure platform.db).

export interface AutopilotRun {
  id: number
  run_at: string | null
  triggered_by: string
  model_filter: string | null
  dry_run: number
  retrains_triggered: number
  promotions_made: number
  policy_blocks: number
  human_required: number
  skipped: number
  summary: string | null
}

export interface AutopilotStatus {
  enabled: boolean
  /** Runtime env override (EXAMLOPS_AUTOPILOT_ENABLED); null unless set. */
  envOverride: boolean | null
  /** Effective state = env override if set, else the persistent config. */
  effective: boolean
  recentRuns: AutopilotRun[]
}

export const useAutopilotStatus = () =>
  useQuery<AutopilotStatus>({
    queryKey: ['autopilot', 'status'],
    queryFn: () => apiFetch<AutopilotStatus>('/api/autopilot/status'),
  })

export const setAutopilotEnabled = (enabled: boolean): Promise<{ enabled: boolean }> =>
  apiFetch(`/api/autopilot/${enabled ? 'enable' : 'disable'}`, { method: 'POST' })

export const useSetAutopilotEnabled = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (enabled: boolean) => setAutopilotEnabled(enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['autopilot'] }),
  })
}
