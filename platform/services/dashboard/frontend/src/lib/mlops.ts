import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './api'

// ── view types (mirror backend mlops.py / F9 R2 interfaces) ──────────────────

export interface ModelRow {
  name: string
  mlflowName: string
  version: number | null
  stage: string
  /** colourblind-safe token: 'ok' | 'warn' | 'unknown' (F3) */
  health: string
  freshness: string | null
  governed: boolean
}

export interface PromotionCheck {
  model: string
  mlflowName: string
  policy: {
    allow: boolean
    reasons: string[]
    metric?: string
    operator?: string
    threshold?: number
    fromAlias?: string
    toAlias?: string
  }
  eval: EvalGate
  approval: { required: boolean; state?: string }
  allowed: boolean
}

/** One metric's verdict from a persisted eval-gate report (ADR 0008). */
export interface GateMetric {
  name: string
  candidate: number | null
  baseline: number | null
  delta: number | null
  min: number | null
  max_drop: number | null
  max?: number | null
  failed: boolean
  reason?: string
}

export interface GateReport {
  id: number
  candidate: string | null
  baseline: string | null
  passed: boolean
  mode: string
  ts: string
  aggregate: string
  metrics: GateMetric[]
  judge: string | null
  judgeEligible: boolean
  judgeFailures: string[]
  calibrationId: string | null
}

/**
 * The ADR 0008 eval gate's standing, read from the gate reports the platform persisted — never
 * inferred from the promotion policy (which is what the panel used to do).
 */
export interface EvalGate {
  state: 'no_gate' | 'not_run' | 'passed' | 'failed' | 'warned'
  pass: boolean | null
  reason: string
  metrics: GateMetric[]
  lastReport: GateReport | null
  suite?: string
  baselineAlias?: string
  mode?: string
}

/** How the eval gate reads on the panel: a status for the pill and a short label. */
export function evalGateView(g: EvalGate): {
  status: 'healthy' | 'failed' | 'degraded' | 'pending' | 'unknown'
  label: string
} {
  switch (g.state) {
    case 'passed':
      return { status: 'healthy', label: `Eval gate passed (v${g.lastReport?.candidate ?? '?'})` }
    case 'failed':
      return { status: 'failed', label: `Eval gate failed (v${g.lastReport?.candidate ?? '?'})` }
    case 'warned':
      return { status: 'degraded', label: 'Eval gate warning — not blocking' }
    case 'not_run':
      return { status: 'pending', label: 'Eval gate not yet run' }
    default:
      return { status: 'unknown', label: 'No eval gate' }
  }
}

export interface RegistryResponse {
  registry: { rows: ModelRow[]; count: number }
  _partial?: string[]
}

// ── pure helpers (unit-tested — the display logic F9 R4 hinges on) ────────────

/**
 * One-line, human verdict for the guided-promotion gate (F9 R4). A denied promotion
 * always surfaces *why*; an allowed one still names the pending approval step so the
 * human gate is never hidden.
 */
export function promotionVerdict(chk: PromotionCheck): { tone: 'ok' | 'warn'; text: string } {
  if (!chk.allowed) {
    const why = chk.policy.reasons[0] ?? 'gate failed'
    return { tone: 'warn', text: `Blocked — ${why}` }
  }
  if (chk.approval.required) {
    return { tone: 'warn', text: 'Eligible — awaiting approval' }
  }
  return { tone: 'ok', text: 'Ready to promote' }
}

/** Freshness → short relative-ish label; null when never recorded. */
export function freshnessLabel(iso: string | null): string {
  if (!iso) return '—'
  // Backend stores ISO/SQLite timestamps; show date + HH:MM without pulling in a date lib.
  return iso.replace('T', ' ').slice(0, 16)
}

// ── data hooks ────────────────────────────────────────────────────────────────

export const useMlopsRegistry = () =>
  useQuery<RegistryResponse>({
    queryKey: ['mlops', 'registry'],
    queryFn: () => apiFetch<RegistryResponse>('/api/v1/mlops/registry'),
  })

export const useGateReports = (name: string | null) =>
  useQuery<{ reports: GateReport[] }>({
    queryKey: ['mlops', 'gate-reports', name],
    queryFn: () => apiFetch<{ reports: GateReport[] }>(`/api/v1/mlops/gate-reports/${name}`),
    enabled: !!name,
  })

export const useMlopsPromotion = (name: string | null) =>
  useQuery<{ promotion: PromotionCheck }>({
    queryKey: ['mlops', 'promotion', name],
    queryFn: () => apiFetch<{ promotion: PromotionCheck }>(`/api/v1/mlops/promotion/${name}`),
    enabled: !!name,
  })
