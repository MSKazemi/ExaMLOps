import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// ── view types (mirror the backend projects router) ──────────────────────────
//
// The Projects console maps the platform's project/namespace model onto the dashboard: quota,
// assigned resources, members, and budget vs. consumption. The BFF remains the sole enforcement
// point (F15) — these types only shape what the UI renders.

export interface ProjectQuota {
  cpuLimit: number
  memoryLimitGb: number
  storageGb: number
  gpuLimit: number
}

export interface ProjectSummary {
  name: string
  description: string
  status: string
  quota: ProjectQuota
  modelCount: number
  resourceCount: number
  memberCount: number
}

export interface ProjectMember {
  subject: string
  role: string
  grantedBy: string
  when: string
}

export interface ProjectBudget {
  gpuHours: number
  costUsd: number
}

export interface ProjectConsumption {
  gpu_hours: number
  cost_usd: number
}

export interface ProjectDetail {
  name: string
  description: string
  status: string
  quota: ProjectQuota
  resources: Record<string, string[]>
  members: ProjectMember[]
  budget: ProjectBudget | null
  consumption: ProjectConsumption
  createdAt: string
  createdBy: string
}

// ── request bodies (admin-only mutations) ────────────────────────────────────

export interface CreateProjectBody {
  name: string
  description: string
  cpuLimit: number
  memoryLimitGb: number
  storageGb: number
  gpuLimit: number
}

export interface AssignResourceBody {
  kind: string
  ref: string
}

export interface AddMemberBody {
  subject: string
  role: string
}

/** Resource kinds a project can hold (mirrors the backend enum). */
export const RESOURCE_KINDS = [
  'model',
  'pipeline',
  'serving_endpoint',
  'connection',
  'dataset',
  'storage',
] as const

/** Member roles (mirrors the backend enum). */
export const MEMBER_ROLES = ['owner', 'editor', 'viewer'] as const

// ── pure helpers (unit-tested — the display logic hinges on these) ────────────

/** Project status → colourblind-safe F3 token. Pure. */
export function statusToken(status: string): 'ok' | 'warn' | 'critical' | 'unknown' {
  switch (status) {
    case 'active':
      return 'ok'
    case 'suspended':
      return 'warn'
    case 'archived':
      return 'critical'
    default:
      return 'unknown'
  }
}

/** One-line quota summary (CPU / mem / storage / GPU). Pure. */
export function quotaSummary(q: ProjectQuota): string {
  return `${q.cpuLimit} CPU · ${q.memoryLimitGb} GB · ${q.storageGb} GB · ${q.gpuLimit} GPU`
}

/**
 * Budget headroom as a ratio in [0, 1] of GPU-hours consumed against budget; null when no budget
 * is set (so the UI shows "no budget" rather than a misleading 0%). Pure.
 */
export function budgetUsage(
  budget: ProjectBudget | null,
  consumption: ProjectConsumption,
): number | null {
  if (!budget || budget.gpuHours <= 0) return null
  return consumption.gpu_hours / budget.gpuHours
}

// ── plain fetchers ────────────────────────────────────────────────────────────

export const listProjects = (): Promise<ProjectSummary[]> =>
  apiFetch<ProjectSummary[]>('/api/v1/projects')

export const getProject = (name: string): Promise<ProjectDetail> =>
  apiFetch<ProjectDetail>(`/api/v1/projects/${encodeURIComponent(name)}`)

export const createProject = (body: CreateProjectBody): Promise<ProjectDetail> =>
  apiFetch<ProjectDetail>('/api/v1/projects', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const assignResource = (name: string, body: AssignResourceBody): Promise<ProjectDetail> =>
  apiFetch<ProjectDetail>(`/api/v1/projects/${encodeURIComponent(name)}/resources`, {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const addMember = (name: string, body: AddMemberBody): Promise<ProjectDetail> =>
  apiFetch<ProjectDetail>(`/api/v1/projects/${encodeURIComponent(name)}/members`, {
    method: 'POST',
    body: JSON.stringify(body),
  })

// ── data hooks ────────────────────────────────────────────────────────────────

export const useProjects = () =>
  useQuery<ProjectSummary[]>({
    queryKey: ['projects'],
    queryFn: listProjects,
  })

export const useProject = (name: string) =>
  useQuery<ProjectDetail>({
    queryKey: ['projects', 'detail', name],
    queryFn: () => getProject(name),
    enabled: !!name,
  })

export const useCreateProject = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: CreateProjectBody) => createProject(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['projects'] })
    },
  })
}

export const useAssignResource = (name: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: AssignResourceBody) => assignResource(name, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['projects', 'detail', name] })
      qc.invalidateQueries({ queryKey: ['projects'] })
    },
  })
}

export const useAddMember = (name: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: AddMemberBody) => addMember(name, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['projects', 'detail', name] })
      qc.invalidateQueries({ queryKey: ['projects'] })
    },
  })
}
