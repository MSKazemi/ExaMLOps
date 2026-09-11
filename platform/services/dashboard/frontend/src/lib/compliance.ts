import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// EU AI Act edit-parity (ADR 0012). Writes go through the dashboard's compliance router, which reuses
// the shared examlops.compliance code path (validation + conformity state-machine + audited).

// Mirror the backend vocabulary (examlops.compliance.RISK_TIERS / CONFORMITY_STATES) — stable enums.
export const RISK_TIERS = ['prohibited', 'high', 'limited', 'minimal'] as const
export const CONFORMITY_STATES = ['draft', 'documented', 'assessed', 'declared'] as const
export type RiskTier = (typeof RISK_TIERS)[number]
export type ConformityState = (typeof CONFORMITY_STATES)[number]

export interface ComplianceSystem {
  model: string
  tenant: string
  in_scope: number
  risk_tier: string | null
  intended_purpose: string | null
  deployment_context: string | null
  conformity_state: string
  updated_at: string | null
  updated_by: string | null
}

export const useComplianceSystems = () =>
  useQuery<ComplianceSystem[]>({
    queryKey: ['compliance', 'systems'],
    queryFn: () => apiFetch<ComplianceSystem[]>('/api/compliance/systems'),
  })

export interface ClassifyBody {
  riskTier: string
  intendedPurpose?: string
  deploymentContext?: string
}

export const classifySystem = (model: string, body: ClassifyBody) =>
  apiFetch(`/api/compliance/classify/${encodeURIComponent(model)}`, {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const setConformity = (model: string, state: string) =>
  apiFetch(`/api/compliance/conformity/${encodeURIComponent(model)}`, {
    method: 'POST',
    body: JSON.stringify({ state }),
  })

function invalidate(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ['compliance'] })
}

export const useClassifySystem = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ model, body }: { model: string; body: ClassifyBody }) => classifySystem(model, body),
    onSuccess: () => invalidate(qc),
  })
}

export const useSetConformity = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ model, state }: { model: string; state: string }) => setConformity(model, state),
    onSuccess: () => invalidate(qc),
  })
}

// ── Compliance page: Annex-IV technical file + evidence sufficiency (ADR 0012 cl.4, ADR 0110) ──

/** ADR 0110 decision 6: whether a section's evidence can be relied on, not only whether it exists. */
export type SufficiencyStatus = 'verified' | 'unverified' | 'insufficient' | 'missing'

export interface TechnicalFileSection {
  key: string
  title: string
  annexIv: string
  present: boolean
  status: SufficiencyStatus
  reasons: string[]
  content: string
}

export interface TechnicalFile {
  model: string
  tenant: string
  disclaimer: string
  gaps: number
  missing: number
  insufficient: number
  unverified: number
  auditChain: string | null
  telemetryAnchors: string | null
  sections: TechnicalFileSection[]
}

export interface TechnicalFileVersion {
  version: number
  gaps: number
  generated_at: string
  generated_by: string | null
}

export interface Art12Coverage {
  model: string
  total_events: number
  coverage: Record<string, boolean>
  uncovered: string[]
  coverage_pct: number
}

/** Status pill vocabulary for a sufficiency status — colour is never the only cue (label + icon). */
export const SUFFICIENCY: Record<SufficiencyStatus, { pill: string; label: string }> = {
  verified: { pill: 'healthy', label: 'Verified' },
  unverified: { pill: 'warn', label: 'Not tamper-evident' },
  insufficient: { pill: 'critical', label: 'Insufficient' },
  missing: { pill: 'critical', label: 'Missing' },
}

const enc = encodeURIComponent

export const useTechnicalFile = (model: string | null) =>
  useQuery<TechnicalFile>({
    queryKey: ['compliance', 'technical-file', model],
    queryFn: () => apiFetch<TechnicalFile>(`/api/compliance/technical-file/${enc(model ?? '')}`),
    enabled: !!model,
  })

export const useTechnicalFileVersions = (model: string | null) =>
  useQuery<TechnicalFileVersion[]>({
    queryKey: ['compliance', 'technical-files', model],
    queryFn: () => apiFetch<TechnicalFileVersion[]>(`/api/compliance/technical-files/${enc(model ?? '')}`),
    enabled: !!model,
  })

export const useArt12 = (model: string | null) =>
  useQuery<Art12Coverage>({
    queryKey: ['compliance', 'art12', model],
    queryFn: () => apiFetch<Art12Coverage>(`/api/compliance/art12/${enc(model ?? '')}`),
    enabled: !!model,
  })

export const useSaveTechnicalFile = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (model: string) =>
      apiFetch<{ model: string; version: number; gaps: number }>(
        `/api/compliance/technical-file/${enc(model)}`,
        { method: 'POST' },
      ),
    onSuccess: () => invalidate(qc),
  })
}
