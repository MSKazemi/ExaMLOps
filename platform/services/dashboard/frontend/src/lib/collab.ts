import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Collaboration client (F22 / ADR 0073). Comments/annotations on entities + shareable snapshots, via the
// tenant-scoped, audited BFF. Pure helpers are unit-tested; hooks are thin TanStack wrappers.

export interface Comment {
  id: number
  author: string
  body: string
  mentions: string[]
  created_at: string
}

export interface SnapshotResponse {
  token: string
  expires_at: string
}

const MENTION = /@([A-Za-z0-9._-]+)/g

/** Unique @-mentions in a draft, order-preserving (mirrors the backend, for live UI hints). */
export function extractMentions(text: string): string[] {
  const seen: string[] = []
  for (const m of text.matchAll(MENTION)) {
    if (!seen.includes(m[1])) seen.push(m[1])
  }
  return seen
}

/** Absolute, shareable URL for a snapshot token (opens the frozen view, R2). */
export function snapshotShareUrl(token: string, origin = typeof location !== 'undefined' ? location.origin : ''): string {
  return `${origin}/?snapshot=${encodeURIComponent(token)}`
}

const base = (type: string, id: string) => `/api/v1/collab/${encodeURIComponent(type)}/${encodeURIComponent(id)}`

export function useComments(entityType: string, entityId: string) {
  return useQuery<{ comments: Comment[] }>({
    queryKey: ['collab', 'comments', entityType, entityId],
    queryFn: () => apiFetch<{ comments: Comment[] }>(`${base(entityType, entityId)}/comments`),
  })
}

export function useAddComment(entityType: string, entityId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: string) =>
      apiFetch<Comment>(`${base(entityType, entityId)}/comments`, {
        method: 'POST',
        body: JSON.stringify({ body }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['collab', 'comments', entityType, entityId] }),
  })
}

export function useCreateSnapshot() {
  return useMutation({
    mutationFn: (view: Record<string, unknown>) =>
      apiFetch<SnapshotResponse>('/api/v1/collab/snapshot', {
        method: 'POST',
        body: JSON.stringify({ view }),
      }),
  })
}
