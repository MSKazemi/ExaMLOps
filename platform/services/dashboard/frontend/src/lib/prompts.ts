import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Prompt registry (B1). Writes reuse the dashboard prompts router → shared examlops.data.prompts.
// Versions are immutable; labels move (dev/staging/prod). Variables are auto-declared from {tokens}.

export interface PromptVersion {
  version: number
  variables: string[]
  actor: string | null
  created_at: string | null
}
export interface PromptLabel {
  label: string
  version: number
  updated_at: string | null
}
export interface Prompt {
  name: string
  versions: PromptVersion[]
  labels: PromptLabel[]
}

export const usePrompts = () =>
  useQuery<Prompt[]>({ queryKey: ['prompts'], queryFn: () => apiFetch<Prompt[]>('/api/prompts') })

export interface CreateVersionBody {
  template: string
  label?: string
}
export interface CreateVersionResult {
  name: string
  version: number
  variables: string[]
  label: string | null
}

export const createPromptVersion = (name: string, body: CreateVersionBody): Promise<CreateVersionResult> =>
  apiFetch<CreateVersionResult>(`/api/prompts/${encodeURIComponent(name)}/versions`, {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const setPromptLabel = (name: string, label: string, version: number) =>
  apiFetch(`/api/prompts/${encodeURIComponent(name)}/label`, {
    method: 'POST',
    body: JSON.stringify({ label, version }),
  })

function invalidate(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ['prompts'] })
}

export const useCreatePromptVersion = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, body }: { name: string; body: CreateVersionBody }) => createPromptVersion(name, body),
    onSuccess: () => invalidate(qc),
  })
}

export const useSetPromptLabel = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, label, version }: { name: string; label: string; version: number }) =>
      setPromptLabel(name, label, version),
    onSuccess: () => invalidate(qc),
  })
}
