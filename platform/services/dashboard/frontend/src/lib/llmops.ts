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

/** One LLMOps calculation domain and the provider that computes it (ADR 0083). */
export interface CalculationProvider {
  domain: string
  /** null when no provider runs: the caller's own built-in arithmetic computes the figure. */
  provider: string | null
  /** true = an operator chose it (env / providers.yaml); false = nobody chose. */
  selected: boolean
  default: string | null
  /** 'provider' = a provider computes it; 'builtin' = the caller's own math (nothing selected). */
  mode?: 'provider' | 'builtin'
  ok: boolean
  error: string | null
  version?: string
  methodology?: string
  uncertainty?: number | null
  units?: Record<string, string>
  outputs?: string[]
  params?: string[]
  source?: string
}

export interface LlmopsOverview {
  endpoints?: { rows: LlmEndpoint[]; count: number }
  evals?: { models: EvalModel[]; count: number }
  calculations?: { rows: CalculationProvider[]; count: number; available: boolean }
  _partial?: string[]
}

/** Human label for how a calculation provider was chosen. */
export function providerOriginLabel(p: CalculationProvider): string {
  if (!p.ok) return 'failed to load'
  if (p.selected) return 'configured'
  return p.mode === 'builtin' ? 'built-in' : 'default'
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
