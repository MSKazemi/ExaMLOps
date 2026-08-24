import type { ServiceStatus } from '@/lib/api'

interface Props {
  name: string
  url: string
  status: ServiceStatus
  /** Why an `unknown` entry is not measured; shown on hover. */
  note?: string
}

const STATUS_CONFIG: Record<ServiceStatus, {
  dot: string
  ring: string
  label: string
  labelColor: string
  bg: string
  border: string
}> = {
  ok: {
    dot: '#10b981',
    ring: '#10b981',
    label: 'Online',
    labelColor: 'var(--success-text)',
    bg: 'oklch(0.72 0.18 155 / 8%)',
    border: 'oklch(0.72 0.18 155 / 25%)',
  },
  degraded: {
    dot: '#f59e0b',
    ring: '#f59e0b',
    label: 'Degraded',
    labelColor: 'var(--warning-text)',
    bg: 'oklch(0.78 0.18 80 / 8%)',
    border: 'oklch(0.78 0.18 80 / 25%)',
  },
  down: {
    dot: '#ef4444',
    ring: '#ef4444',
    label: 'Offline',
    labelColor: 'var(--error-text)',
    bg: 'oklch(0.66 0.22 25 / 8%)',
    border: 'oklch(0.66 0.22 25 / 25%)',
  },
  // Deliberately grey, and deliberately not red: nothing was measured, so nothing is claimed.
  unknown: {
    dot: '#94a3b8',
    ring: '#94a3b8',
    label: 'Not measured',
    labelColor: 'var(--muted-foreground)',
    bg: 'oklch(0.60 0.02 260 / 8%)',
    border: 'oklch(0.60 0.02 260 / 25%)',
  },
}

export function ServiceCard({ name, url, status, note }: Props) {
  // The status arrives from the API as data, so TypeScript's exhaustiveness proves nothing about
  // it: a backend that adds a fifth word would index this map to `undefined` and the first
  // `cfg.bg` below would take the whole page down. Fall back to the one entry that claims least.
  const cfg = STATUS_CONFIG[status] ?? STATUS_CONFIG.unknown

  return (
    <div
      className="rounded-xl p-4 card-hover flex flex-col gap-3"
      style={{
        background: 'var(--surface-0)',
        border: '1px solid var(--border)',
      }}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="font-semibold text-sm leading-tight">{name}</span>
        {/* Status badge */}
        <div
          className="flex items-center gap-1.5 rounded-full px-2.5 py-1 shrink-0"
          style={{ background: cfg.bg, border: `1px solid ${cfg.border}` }}
          title={note}
        >
          {/* Pulse dot */}
          <span className="relative flex h-1.5 w-1.5 shrink-0">
            {status === 'ok' && (
              <span
                className="animate-ping absolute inline-flex h-full w-full rounded-full opacity-75"
                style={{ backgroundColor: cfg.ring }}
              />
            )}
            <span
              className="relative inline-flex rounded-full h-1.5 w-1.5"
              style={{ backgroundColor: cfg.dot }}
            />
          </span>
          <span className="text-[11px] font-medium" style={{ color: cfg.labelColor }}>
            {cfg.label}
          </span>
        </div>
      </div>

      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-muted-foreground font-mono truncate max-w-[160px]">
          {url}
        </span>
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className="shrink-0 text-xs font-medium px-2.5 py-1 rounded-md transition-all duration-150"
          style={{
            background: 'oklch(0.64 0.20 265 / 15%)',
            border: '1px solid oklch(0.64 0.20 265 / 25%)',
            color: 'var(--accent-text)',
          }}
          onMouseEnter={e => {
            (e.currentTarget as HTMLElement).style.background = 'oklch(0.64 0.20 265 / 25%)'
          }}
          onMouseLeave={e => {
            (e.currentTarget as HTMLElement).style.background = 'oklch(0.64 0.20 265 / 15%)'
          }}
        >
          Open →
        </a>
      </div>
    </div>
  )
}
