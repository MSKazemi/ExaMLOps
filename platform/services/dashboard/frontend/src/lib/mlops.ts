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
  eval: { pass: boolean; metrics: Record<string, number> }
  approval: { required: boolean; state?: string }
  allowed: boolean
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

export const useMlopsPromotion = (name: string | null) =>
  useQuery<{ promotion: PromotionCheck }>({
    queryKey: ['mlops', 'promotion', name],
    queryFn: () => apiFetch<{ promotion: PromotionCheck }>(`/api/v1/mlops/promotion/${name}`),
    enabled: !!name,
  })
