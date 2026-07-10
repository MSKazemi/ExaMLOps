import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './api'

// ── view types (mirror backend governance.py / F14 interfaces) ───────────────

export type PostureStatus = 'satisfied' | 'partial' | 'gap'

export interface ControlCoverage {
  control: string
  function: string
  title: string
  status: PostureStatus
  evidence: string[]
}

export interface ComplianceRow {
  model: string
  version: number | null
  riskClass: string
  technicalFile: boolean
  provenance: boolean
}

export interface GovernanceOverview {
  posture?: { controls: ControlCoverage[]; satisfied: number; total: number }
  compliance?: { rows: ComplianceRow[]; count: number }
  cards?: { withCard: string[]; withoutCard: string[]; coverage: number | null; total: number }
  audit?: { count: number; headDigest: string | null; verified: boolean; entries: unknown[] }
  _partial?: string[]
}

// ── pure helpers (unit-tested) ────────────────────────────────────────────────

/** Map a posture status to a colour-blind-safe F3 status token (satisfied/partial/gap). */
export function postureToken(status: PostureStatus): 'ok' | 'warn' | 'critical' {
  return status === 'satisfied' ? 'ok' : status === 'partial' ? 'warn' : 'critical'
}

/** Coverage ratio → percent label (honest: null → "no models"). */
export function coverageLabel(coverage: number | null): string {
  if (coverage === null) return 'no models'
  return `${Math.round(coverage * 100)}%`
}

/** Short tamper-evidence anchor from the head digest (first 12 hex chars). */
export function digestShort(digest: string | null): string {
  return digest ? digest.slice(0, 12) : '—'
}

// ── data hook ─────────────────────────────────────────────────────────────────

export const useGovernance = () =>
  useQuery<GovernanceOverview>({
    queryKey: ['governance', 'overview'],
    queryFn: () => apiFetch<GovernanceOverview>('/api/v1/governance/overview'),
  })
