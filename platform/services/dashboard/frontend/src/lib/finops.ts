import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './api'

// ── view types (mirror backend finops.py / F13 interfaces) ───────────────────

export interface CostRow {
  dimension: string
  key: string
  gpuHours: number
  costUsd: number
  runs: number
}

export interface BudgetRow {
  project: string
  period: string
  gpuHoursBudget: number | null
  costBudget: number | null
  gpuHoursRatio: number | null
  costRatio: number | null
  overBudget: boolean
}

export interface FinopsOverview {
  cost?: { rows: CostRow[]; total_gpu_hours: number; total_cost_usd: number }
  budget?: { budgets: BudgetRow[]; consumed_gpu_hours: number; consumed_cost_usd: number }
  carbon?: {
    totals: { kwh: number; co2e_g: number; records: number }
    byModel: { model: string; kwh: number; co2e_g: number }[]
    co2e_kg: number
    /** ADR 0112 R-ee: every figure is operational; embodied carbon is not measured. */
    scope?: 'operational'
    embodiedKg?: number | null
    scopeNote?: string
    uncertainty: number
    methodology: string
  }
  unitEconomics?: { costPerTrainingRun: number | null; inference: unknown }
  _partial?: string[]
}

// ── pure helpers (unit-tested) ────────────────────────────────────────────────

/** Format a USD amount for display. */
export function usd(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/** A budget usage ratio → percent label, or "no budget". */
export function budgetPct(ratio: number | null): string {
  if (ratio === null) return 'no budget'
  if (!isFinite(ratio)) return '∞'
  return `${Math.round(ratio * 100)}%`
}

/**
 * CO₂e with its uncertainty band → a "kg ±%" label so the UI never shows false precision (F13 R3).
 */
export function carbonLabel(co2eKg: number, uncertainty: number): string {
  return `${co2eKg.toLocaleString(undefined, { maximumFractionDigits: 2 })} kg ±${Math.round(
    uncertainty * 100,
  )}%`
}

// ── data hook ─────────────────────────────────────────────────────────────────

export const useFinops = () =>
  useQuery<FinopsOverview>({
    queryKey: ['finops', 'overview'],
    queryFn: () => apiFetch<FinopsOverview>('/api/v1/finops/overview'),
  })
