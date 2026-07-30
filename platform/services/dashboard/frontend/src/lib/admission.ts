import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Admission console (Phase 1 item 1.5). Reads/writes reuse the dashboard admission router →
// shared examlops.admission.stats / examlops.admission.submit (pure platform.db). The durable,
// per-tenant fair-share admission-control queue — enqueue + queue-depth only, no live drain.

export interface AdmissionStats {
  queued: number
  running: number
  done: number
  rejected: number
  failed: number
}

export interface AdmissionView {
  stats: AdmissionStats
  total: number
}

export interface SubmitAdmissionBody {
  kind: string
  payload?: string
  tenant?: string
  project?: string
  priority?: number
}

export interface SubmitAdmissionResult {
  id: number
  kind: string
  tenant: string
  priority: number
}

export const useAdmission = () =>
  useQuery<AdmissionView>({
    queryKey: ['admission'],
    queryFn: () => apiFetch<AdmissionView>('/api/v1/admission'),
  })

export const submitAdmission = (body: SubmitAdmissionBody) =>
  apiFetch<SubmitAdmissionResult>('/api/v1/admission', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const useSubmitAdmission = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: SubmitAdmissionBody) => submitAdmission(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admission'] }),
  })
}
