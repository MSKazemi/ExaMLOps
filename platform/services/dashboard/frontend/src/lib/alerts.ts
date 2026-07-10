import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// ── view types (mirror backend alerts.py / F12 interfaces) ───────────────────

export type AlertSeverity = 'critical' | 'error' | 'warn' | 'info'

export interface Alert {
  id: string
  source: 'drift' | 'budget' | 'eval' | string
  severity: AlertSeverity
  title: string
  labels: Record<string, string>
  state: string
}

export interface AlertInbox {
  inbox?: { alerts: Alert[]; count: number; counts: Record<string, number> }
  _partial?: string[]
}

// ── pure helpers (unit-tested) ────────────────────────────────────────────────

const SEVERITY_RANK: Record<string, number> = { critical: 0, error: 1, warn: 2, info: 3 }

/** Sort key for a severity (most severe first). */
export function severityRank(severity: string): number {
  return SEVERITY_RANK[severity] ?? 9
}

/** Map an alert severity onto an F3 status token. */
export function severityToken(severity: AlertSeverity): 'critical' | 'warn' | 'info' {
  if (severity === 'critical' || severity === 'error') return 'critical'
  if (severity === 'warn') return 'warn'
  return 'info'
}

/** Summarize the inbox counts into a short headline, e.g. "1 critical · 2 warnings". */
export function inboxHeadline(counts: Record<string, number>): string {
  const parts: string[] = []
  if (counts.critical) parts.push(`${counts.critical} critical`)
  if (counts.error) parts.push(`${counts.error} error${counts.error > 1 ? 's' : ''}`)
  if (counts.warn) parts.push(`${counts.warn} warning${counts.warn > 1 ? 's' : ''}`)
  return parts.length ? parts.join(' · ') : 'No active alerts'
}

// ── data hooks ────────────────────────────────────────────────────────────────

export const useAlerts = () =>
  useQuery<AlertInbox>({
    queryKey: ['alerts'],
    queryFn: () => apiFetch<AlertInbox>('/api/v1/alerts'),
    refetchInterval: 20_000,
  })

export const useAckAlert = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<{ acked: boolean }>(`/api/v1/alerts/${encodeURIComponent(id)}/ack`, { method: 'POST' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['alerts'] }),
  })
}
