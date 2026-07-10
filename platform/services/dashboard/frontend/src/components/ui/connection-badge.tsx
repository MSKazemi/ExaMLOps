import { StatusPill } from './status-pill'
import type { ConnectionState } from '@/lib/realtime'

/** Map the realtime connection state onto a colourblind-safe status (reuses F3 semantics). */
const STATE_META: Record<ConnectionState, { status: string; label: string }> = {
  live: { status: 'healthy', label: 'Live' },
  reconnecting: { status: 'pending', label: 'Reconnecting…' },
  polling: { status: 'warn', label: 'Polling (offline)' },
}

export interface ConnectionBadgeProps {
  state: ConnectionState
  className?: string
}

/**
 * ConnectionBadge — shows whether a live surface is streaming, reconnecting, or has fallen back
 * to polling (F8 R5). Built on StatusPill so the state is conveyed by icon + label, never colour
 * alone (F18 / WCAG 2.2 AA).
 */
export function ConnectionBadge({ state, className }: ConnectionBadgeProps) {
  const meta = STATE_META[state]
  return <StatusPill status={meta.status} label={meta.label} className={className} />
}
