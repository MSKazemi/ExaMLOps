import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// ── view types (mirror the backend workbenches router) ───────────────────────
//
// Workbenches (P5, ADR 0090) are project-scoped interactive environments (the notebook a project
// owns). Viewers see status; admins can create, start/stop, and delete. Every mutation reuses the
// same `examlops.workbenches` code path as `exa workbench create`, so the UI can't drift from the CLI.

export type WorkbenchStatus = 'RUNNING' | 'STOPPED'

export interface WorkbenchSummary {
  name: string
  project: string
  image: string
  cpu: number | null
  memoryGb: number | null
  volume: string
  status: WorkbenchStatus
  createdAt: string
  createdBy: string
  /** JupyterHub Open URL when RUNNING (null when the Hub isn't wired). */
  url?: string | null
  /** Hardware profile + the exact version this workbench was created from (ADR 0157). */
  hardwareProfile?: string | null
  hardwareProfileVersion?: number | null
}

export interface SetWorkbenchStatusBody {
  name: string
  status: WorkbenchStatus
}

export interface CreateWorkbenchBody {
  name: string
  image?: string
  cpu?: number
  memoryGb?: number
  /** Named hardware profile supplying cpu/memory defaults (applicable to workbench or any). */
  hardwareProfile?: string
}

/** Launch spec returned when a workbench is started (image, mounted volume, injected env keys). */
export interface WorkbenchStatusResult {
  project: string
  name: string
  status: WorkbenchStatus
  image?: string
  volume?: string
  injectedEnv?: string[]
  url?: string | null
}

// ── pure helper (unit-tested) ─────────────────────────────────────────────────

/** The status a Start/Stop toggle should move a workbench to. Pure. */
export function nextStatus(status: WorkbenchStatus): WorkbenchStatus {
  return status === 'RUNNING' ? 'STOPPED' : 'RUNNING'
}

// ── plain fetchers ────────────────────────────────────────────────────────────

export const listWorkbenches = (project: string): Promise<WorkbenchSummary[]> =>
  apiFetch<WorkbenchSummary[]>(`/api/v1/workbenches?project=${encodeURIComponent(project)}`)

export const setWorkbenchStatus = (
  project: string,
  name: string,
  status: WorkbenchStatus,
): Promise<WorkbenchStatusResult> =>
  apiFetch(`/api/v1/workbenches/${encodeURIComponent(project)}/${encodeURIComponent(name)}/status`, {
    method: 'POST',
    body: JSON.stringify({ status }),
  })

export const createWorkbench = (
  project: string,
  body: CreateWorkbenchBody,
): Promise<WorkbenchSummary> =>
  apiFetch<WorkbenchSummary>('/api/v1/workbenches', {
    method: 'POST',
    body: JSON.stringify({ project, ...body }),
  })

export const deleteWorkbench = (
  project: string,
  name: string,
): Promise<{ project: string; name: string; deleted: boolean }> =>
  apiFetch(`/api/v1/workbenches/${encodeURIComponent(project)}/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  })

// ── data hooks ────────────────────────────────────────────────────────────────

export const useWorkbenches = (project: string) =>
  useQuery<WorkbenchSummary[]>({
    queryKey: ['workbenches', project],
    queryFn: () => listWorkbenches(project),
    enabled: !!project,
  })

export const useSetWorkbenchStatus = (project: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, status }: SetWorkbenchStatusBody) =>
      setWorkbenchStatus(project, name, status),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['workbenches', project] })
    },
  })
}

export const useCreateWorkbench = (project: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: CreateWorkbenchBody) => createWorkbench(project, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['workbenches', project] })
      qc.invalidateQueries({ queryKey: ['projects', 'detail', project] })
    },
  })
}

export const useDeleteWorkbench = (project: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name: string) => deleteWorkbench(project, name),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['workbenches', project] })
      qc.invalidateQueries({ queryKey: ['projects', 'detail', project] })
    },
  })
}
