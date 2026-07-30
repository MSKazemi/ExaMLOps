import { useMe } from './api'

// Capability affordances (F15 / ADR 0057) — the UI mirror of the backend capability model.
//
// The BFF is the SOLE authorization enforcement point; these helpers only decide UI *affordances*
// (show / disable + explain), never trust. The capability list comes from `/api/auth/me`, so the UI
// automatically reflects whatever the backend grants — no hardcoded role→capability table on the
// client beyond the human explanations below.

export const CAP = {
  VIEW: 'view',
  SEARCH: 'search',
  MODEL_PROMOTE: 'model.promote',
  APPROVAL_DECIDE: 'approval.decide',
  RETRAIN_TRIGGER: 'retrain.trigger',
  DRIFT_BASELINE: 'drift.baseline',
  CONFIG_WRITE: 'config.write',
  SECRET_REVEAL: 'secret.reveal',
  SERVICE_CONTROL: 'service.control',
  PROJECT_MANAGE: 'project.manage',
  CONNECTION_MANAGE: 'connection.manage',
  PROVIDERS_MANAGE: 'providers.manage',
  SCALING_MANAGE: 'scaling.manage',
  ADMISSION_MANAGE: 'admission.manage',
  EVENTS_MANAGE: 'events.manage',
  TRAFFIC_MANAGE: 'traffic.manage',
} as const

export type Capability = (typeof CAP)[keyof typeof CAP]

/** Actions that additionally require step-up/MFA before the BFF permits them (F15 R6 / F16). */
const STEP_UP: ReadonlySet<string> = new Set([CAP.MODEL_PROMOTE, CAP.SECRET_REVEAL])

export function requiresStepUp(capability: string): boolean {
  return STEP_UP.has(capability)
}

/** Whether a capability set grants `capability`. Pure. */
export function can(capabilities: readonly string[] | undefined, capability: string): boolean {
  return !!capabilities && capabilities.includes(capability)
}

/** Human explanation for a denied capability (F15 R3 — never a silent dead control). Pure. */
export function reason(capabilities: readonly string[] | undefined, capability: string): string {
  if (can(capabilities, capability)) return ''
  return 'You do not have permission for this action (requires elevated role).'
}

export interface CapabilityContext {
  capabilities: string[]
  tenant: string
  can: (capability: string) => boolean
  reason: (capability: string) => string
  requiresStepUp: (capability: string) => boolean
}

/** Reactive capability context sourced from `/api/auth/me` (F15). */
export function useCapabilities(): CapabilityContext {
  const { data } = useMe()
  const capabilities = data?.capabilities ?? []
  return {
    capabilities,
    tenant: data?.tenant ?? 'default',
    can: (c) => can(capabilities, c),
    reason: (c) => reason(capabilities, c),
    requiresStepUp,
  }
}
