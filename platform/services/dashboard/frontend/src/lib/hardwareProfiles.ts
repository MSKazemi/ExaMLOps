import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './api'

// ── view types (mirror the backend hardware-profiles router, ADR 0157 Phase 4) ─────────────
//
// A hardware profile is a named, versioned resource+runtime bundle a workbench, a training run or
// a serving deployment references instead of restating raw cpu/memory/GPU numbers. The router
// calls the same `examlops.hardware_profiles` code path as `exa hardware profile`.

export type ProfileApplicability = 'workbench' | 'training' | 'serving' | 'any'

export type ProfileStatus = 'unchecked' | 'verified' | 'degraded' | 'unresolvable' | 'missing'

export interface HardwareProfileSummary {
  name: string
  version: number
  acceleratorFamily: string
  acceleratorModelHint: string | null
  gpuCount: number
  gpuFraction: number
  migProfile: string | null
  cpu: number
  memoryGb: number
  nodes: number
  driverTag: string | null
  runtimeTag: string | null
  applicability: ProfileApplicability[]
  description: string
  createdAt: string | null
  createdBy: string | null
}

export interface InUseEntry {
  consumer: 'workbench' | 'training' | 'serving'
  consumer_ref: string
  project: string | null
  name: string
  version: number
  status: ProfileStatus
  reason: string
  unconfirmed: string[]
  target_cluster: string | null
  resolved_at: string | null
  exists: boolean
}

export interface InUseReport {
  window_days: number
  project: string | null
  entries: InUseEntry[]
  counts: Partial<Record<ProfileStatus, number>>
  attention: InUseEntry[]
}

// ── pure helpers (unit-tested) ──────────────────────────────────────────────────────────────

/** One-line shape of a profile for a select option, e.g. `gpu-small v2 · 4 CPU · 16 GB · 1 GPU`. */
export function profileShape(p: HardwareProfileSummary): string {
  const parts = [`${p.name} v${p.version}`]
  if (p.cpu > 0) parts.push(`${p.cpu} CPU`)
  if (p.memoryGb > 0) parts.push(`${p.memoryGb} GB`)
  if (p.gpuCount > 0) {
    parts.push(p.gpuFraction < 1 ? `${p.gpuCount}×${p.gpuFraction} GPU` : `${p.gpuCount} GPU`)
  }
  return parts.join(' · ')
}

/** Whether a profile may size `need` — the same rule the backend enforces (`any` covers all). */
export function isApplicable(p: HardwareProfileSummary, need: ProfileApplicability): boolean {
  return p.applicability.includes(need) || p.applicability.includes('any')
}

/** Statuses an operator must look at; mirrors `ATTENTION_STATUSES` in examlops.hardware_profiles. */
export function needsAttention(status: ProfileStatus): boolean {
  return status === 'degraded' || status === 'unresolvable' || status === 'missing'
}

// ── fetchers + hooks ─────────────────────────────────────────────────────────────────────────

export const listHardwareProfiles = (
  applicability?: ProfileApplicability,
): Promise<HardwareProfileSummary[]> =>
  apiFetch<HardwareProfileSummary[]>(
    '/api/v1/hardware-profiles' +
      (applicability ? `?applicability=${encodeURIComponent(applicability)}` : ''),
  )

export const getInUse = (project?: string): Promise<InUseReport> =>
  apiFetch<InUseReport>(
    '/api/v1/hardware-profiles/in-use' +
      (project ? `?project=${encodeURIComponent(project)}` : ''),
  )

export const useHardwareProfiles = (applicability?: ProfileApplicability) =>
  useQuery<HardwareProfileSummary[]>({
    queryKey: ['hardware-profiles', applicability ?? 'all'],
    queryFn: () => listHardwareProfiles(applicability),
  })

export const useHardwareProfilesInUse = (project?: string) =>
  useQuery<InUseReport>({
    queryKey: ['hardware-profiles', 'in-use', project ?? 'all'],
    queryFn: () => getInUse(project),
  })
