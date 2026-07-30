import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Platform secrets (D7). Writes reuse the dashboard secrets router → shared examlops.secrets.set_secret
// (encrypts into platform.db). The plaintext value is WRITE-ONLY — the list/API never returns it.

export interface SecretMeta {
  path: string
  tenant: string
  version: number
  updated_by: string | null
  updated_at: string | null
  hasValue: boolean
}

export interface SetSecretBody {
  path: string
  value: string
  tenant?: string
}

export const useSecrets = () =>
  useQuery<SecretMeta[]>({ queryKey: ['secrets'], queryFn: () => apiFetch<SecretMeta[]>('/api/secrets') })

export const setSecret = (body: SetSecretBody): Promise<{ path: string; tenant: string; version: number }> =>
  apiFetch('/api/secrets', { method: 'POST', body: JSON.stringify(body) })

export const useSetSecret = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: SetSecretBody) => setSecret(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['secrets'] }),
  })
}
