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
