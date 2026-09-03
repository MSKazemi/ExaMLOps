import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Champion-challenger console (ADR 0024 clause 4). Every call goes to the dashboard challenger
// router, which delegates to `examlops.champion_challenger` — the same code path `exa serve
// challenger` uses — so the two surfaces cannot disagree about whether a promotion is warranted.

export interface ChallengerConfig {
  model: string
  challenger_version: string
  mirror_pct: number
  enabled: number
  auto_promote?: number
}

/** The scored comparison. `configured: false` means no challenger for this model — not an error. */
export interface ChallengerStatus {
  model: string
  configured: boolean
  challenger_version?: string
  n?: number
  champion_error?: number | null
  challenger_error?: number | null
  /** champion_error − challenger_error; positive means the challenger is better. */
  delta?: number | null
  p_value?: number | null
  significant?: boolean
  slo_ok?: boolean
  slo_reason?: string
  policy_met?: boolean
}

export interface PromoteResult {
  model: string
  proposed: boolean
  /** Present when refused — why the policy was not met. */
  reason?: string
  status?: ChallengerStatus | null
  challengerVersion?: string
  delta?: number
  pValue?: number | null
  n?: number
  auto?: boolean
}

export const useChallengers = () =>
  useQuery<ChallengerConfig[]>({
    queryKey: ['challenger', 'list'],
    queryFn: () => apiFetch<ChallengerConfig[]>('/api/challenger'),
  })

export const useChallengerStatus = (model: string | null) =>
  useQuery<ChallengerStatus>({
    queryKey: ['challenger', 'status', model],
    queryFn: () => apiFetch<ChallengerStatus>(`/api/challenger/${model}`),
    enabled: !!model,
  })

export const usePromoteChallenger = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (model: string) =>
      apiFetch<PromoteResult>(`/api/challenger/${model}/promote`, { method: 'POST' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['challenger'] }),
  })
}

export const useDisableChallenger = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (model: string) =>
      apiFetch<{ model: string; enabled: boolean }>(`/api/challenger/${model}/disable`, {
        method: 'POST',
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['challenger'] }),
  })
}
