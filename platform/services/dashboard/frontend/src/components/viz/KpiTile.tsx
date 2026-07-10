import { statusMeta } from '@/lib/status'
import {
  thresholdTone,
  toneStatus,
  formatDelta,
  sparklinePoints,
  type Threshold,
} from '@/lib/viz'

export interface KpiTileProps {
  label: string
  value: number | string
  /** Signed change vs the previous period. */
  delta?: number
  /** Sparkline series (F4 R2 trend). */
  trend?: number[]
  /** Threshold colouring; applied only when `value` is numeric. */
  threshold?: Threshold
  /** Which direction is "worse" for the threshold (default higher-worse). */
  direction?: 'higher-worse' | 'lower-worse'
  unit?: string
  /** Optional leading icon. */
  icon?: React.ComponentType<{ className?: string }>
}

/**
 * KpiTile — the reusable KPI primitive (F4 R2 / ADR 0055).
 *
 * Value + signed delta + inline sparkline, tinted by an ok/warn/crit **threshold tone** resolved
 * through the F3 status tokens — so the colour is theme-aware and always paired with the numeric
 * value (never colour-only). A single-tile inline chart; no data-table fallback needed (the value
 * is the text).
 */
export function KpiTile({
  label,
  value,
  delta,
  trend,
  threshold,
  direction = 'higher-worse',
  unit = '',
  icon: Icon,
}: KpiTileProps) {
  const numeric = typeof value === 'number'
  const tone = numeric ? thresholdTone(value, threshold, direction) : 'ok'
  const color = `var(${statusMeta(toneStatus(tone)).colorVar})`
  const deltaColor =
    delta === undefined || delta === 0
      ? 'var(--subtle-text)'
      : delta > 0
        ? 'var(--success-text)'
        : 'var(--error-text)'

  return (
    <div
      className="rounded-xl border border-border p-4 space-y-2"
      style={threshold && tone !== 'ok' ? { borderColor: color } : undefined}
    >
      <div className="flex items-baseline justify-between gap-2">
        <p className="text-2xl font-bold leading-none tabular-nums" style={{ color: numeric ? color : undefined }}>
          {value}
          {unit}
        </p>
        {delta !== undefined && (
          <span className="text-xs tabular-nums" style={{ color: deltaColor }}>
            {formatDelta(delta, unit)}
          </span>
        )}
      </div>
      <p className="text-xs text-muted-foreground flex items-center gap-1.5">
        {Icon && <Icon className="size-3.5" />}
        {label}
      </p>
      {trend && trend.length > 1 && (
        <svg
          viewBox="0 0 80 20"
          preserveAspectRatio="none"
          className="w-full h-5"
          aria-hidden="true"
        >
          <polyline
            points={sparklinePoints(trend, 80, 20)}
            fill="none"
            stroke={color}
            strokeWidth="1.5"
            vectorEffect="non-scaling-stroke"
          />
        </svg>
      )}
    </div>
  )
}
