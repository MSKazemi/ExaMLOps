import { cn } from '@/lib/utils'
import { statusMeta, type HealthStatus, type Severity } from '@/lib/status'

export interface StatusPillProps {
  /** A health status, a severity, or any backend string (unknown strings render as "Unknown"). */
  status: HealthStatus | Severity | string
  /** Override the default label (e.g. "2 nodes draining"); defaults to the canonical status label. */
  label?: string
  /** Show the leading icon (default true). Colour is never the only cue, so the label always renders. */
  showIcon?: boolean
  className?: string
}

/**
 * StatusPill — the canonical, colourblind-safe status/severity badge (ADR 0051 / F3, F18).
 *
 * Always renders a text label, and by default a distinct icon, tinted with a theme-aware colour token.
 * Colour is therefore never the sole signal of state, satisfying WCAG 2.2 AA.
 */
export function StatusPill({ status, label, showIcon = true, className }: StatusPillProps) {
  const meta = statusMeta(status)
  const Icon = meta.icon
  const text = label ?? meta.label
  const color = `var(${meta.colorVar})`

  return (
    <span
      role="status"
      aria-label={text}
      data-status={status}
      style={{ color, backgroundColor: `color-mix(in oklch, ${color} 14%, transparent)` }}
      className={cn(
        'inline-flex w-fit items-center gap-1 rounded-4xl px-2 py-0.5 text-xs font-medium whitespace-nowrap',
        className,
      )}
    >
      {showIcon && <Icon aria-hidden="true" className="size-3 shrink-0" />}
      {text}
    </span>
  )
}
