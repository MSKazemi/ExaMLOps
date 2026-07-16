import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// ── view types (mirror the backend workbenches router) ───────────────────────
//
// Workbenches (P5, ADR 0090) are project-scoped interactive environments. The dashboard shows
// them read-mostly: viewers see status; admins can start/stop. Provisioning (image, volume,
// injected connections) stays in the CLI (`exa workbench create`).

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
}

export interface SetWorkbenchStatusBody {
  name: string
  status: WorkbenchStatus
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
): Promise<{ project: string; name: string; status: WorkbenchStatus }> =>
  apiFetch(`/api/v1/workbenches/${encodeURIComponent(project)}/${encodeURIComponent(name)}/status`, {
    method: 'POST',
    body: JSON.stringify({ status }),
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
