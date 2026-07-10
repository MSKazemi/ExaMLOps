import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './api'

// ── view types (mirror backend llmops.py / F10 interfaces) ───────────────────

export interface LlmEndpoint {
  model: string
  engine: string
  hfModelId: string
  maxModelLen: number | null
  tensorParallel: number
  dtype: string
  enabled: boolean
}

export interface EvalMetric {
  metric: string
  value: number
  baseline: number | null
  passed: boolean
}

export interface EvalModel {
  model: string
  suite: string
  status: string
  metrics: EvalMetric[]
  passRate: number | null
}

export interface LlmopsOverview {
  endpoints?: { rows: LlmEndpoint[]; count: number }
  evals?: { models: EvalModel[]; count: number }
  _partial?: string[]
}

// ── pure helpers (unit-tested) ────────────────────────────────────────────────

/** Pass-rate ratio → percent label; null (no metrics) → "no evals". */
export function passRateLabel(rate: number | null): string {
  if (rate === null) return 'no evals'
  return `${Math.round(rate * 100)}%`
}

/** Overall eval tone from a pass rate: all-pass → ok, some → warn, none → critical. */
export function evalTone(rate: number | null): 'ok' | 'warn' | 'critical' | 'unknown' {
  if (rate === null) return 'unknown'
  if (rate >= 1) return 'ok'
  if (rate > 0) return 'warn'
  return 'critical'
}

// ── data hook ─────────────────────────────────────────────────────────────────

export const useLlmops = () =>
  useQuery<LlmopsOverview>({
    queryKey: ['llmops', 'overview'],
    queryFn: () => apiFetch<LlmopsOverview>('/api/v1/llmops/overview'),
  })
