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

// ── Project Anatomy (P6/P7/P8) ────────────────────────────────────────────────

export interface ProjectStorage {
  bucket: string
  prefix: string
  quotaGb: number | null
  usedBytes: number
  connectionRef: string | null
}

export interface ProjectConnectionView {
  name: string
  kind: string
  hasSecret: boolean
}

export interface PrefectPipeline {
  deployments: string[]
  schedule: string | null
  lastRunAt: string | null
  status: string
}

export interface RayServePipeline {
  models: string[]
  traffic: Record<string, Record<string, number>>
  status: string
}

export interface ProjectPipelines {
  prefect: PrefectPipeline | null
  rayserve: RayServePipeline | null
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
  // P8 anatomy (optional — older backends omit them)
  storage?: ProjectStorage | null
  connections?: ProjectConnectionView[]
  pipelines?: ProjectPipelines
}

/** Storage usage as a percentage of quota (0 when no quota). Pure. */
export function storageUsagePct(s: ProjectStorage): number {
  const quotaBytes = (s.quotaGb ?? 0) * 1e9
  if (quotaBytes <= 0) return 0
  return Math.min(100, Math.round((s.usedBytes / quotaBytes) * 100))
}

/** Human GB label for a byte count. Pure. */
export function bytesToGb(bytes: number): string {
  return `${(bytes / 1e9).toFixed(2)} GB`
}

/** Colour-blind-safe token for a pipeline surface status. Pure. */
export function pipelineToken(status: string): 'ok' | 'warn' | 'unknown' {
  if (status === 'healthy') return 'ok'
  if (status === 'degraded') return 'warn'
  return 'unknown'
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

export const removeMember = (
  name: string,
  subject: string,
): Promise<{ project: string; subject: string; removed: number }> =>
  apiFetch(
    `/api/v1/projects/${encodeURIComponent(name)}/members/${encodeURIComponent(subject)}`,
    { method: 'DELETE' },
  )

export const deleteProject = (name: string): Promise<{ name: string; deleted: boolean }> =>
  apiFetch(`/api/v1/projects/${encodeURIComponent(name)}`, { method: 'DELETE' })

export interface BindStorageBody {
  connectionRef?: string
}

export interface BindStorageResult {
  project: string
  bucket: string | null
  prefix: string | null
  connectionRef: string | null
  bound: boolean
}

export const bindStorage = (name: string, body: BindStorageBody): Promise<BindStorageResult> =>
  apiFetch<BindStorageResult>(`/api/v1/projects/${encodeURIComponent(name)}/storage`, {
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

export const useRemoveMember = (name: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (subject: string) => removeMember(name, subject),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['projects', 'detail', name] })
      qc.invalidateQueries({ queryKey: ['projects'] })
    },
  })
}

export const useDeleteProject = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name: string) => deleteProject(name),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['projects'] })
    },
  })
}

export const useBindStorage = (name: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: BindStorageBody) => bindStorage(name, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['projects', 'detail', name] })
    },
  })
}
