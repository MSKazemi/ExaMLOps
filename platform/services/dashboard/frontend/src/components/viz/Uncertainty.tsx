import { ChartFrame } from './ChartFrame'
import { ciLabel } from '@/lib/viz'

export interface UncertaintyVariant {
  label: string
  mean: number
  ci: [number, number]
}

export interface UncertaintyProps {
  variants: UncertaintyVariant[]
  title?: string
  ariaLabel?: string
  unit?: string
}

/**
 * Uncertainty — a point-with-CI ("dot-and-whisker") chart for A/B and eval results (F4 R5).
 *
 * Renders each variant's mean as a dot with a confidence-interval error bar, so results are shown
 * with their uncertainty rather than as bare point estimates (F4 GWT-4). Means + CIs are also in
 * the data-table fallback (F4 R7).
 */
export function Uncertainty({
  variants,
  title,
  ariaLabel = 'Estimates with confidence intervals',
  unit = '',
}: UncertaintyProps) {
  const lo = Math.min(...variants.map((v) => v.ci[0]))
  const hi = Math.max(...variants.map((v) => v.ci[1]))
  const span = hi - lo || 1
  const pct = (x: number) => ((x - lo) / span) * 100

  const table = {
    columns: [
      { key: 'variant', label: 'Variant' },
      { key: 'mean', label: 'Mean' },
      { key: 'ci', label: '95% CI' },
    ],
    rows: variants.map((v) => ({
      variant: v.label,
      mean: `${v.mean.toFixed(2)}${unit}`,
      ci: ciLabel(v.ci),
    })),
  }

  return (
    <ChartFrame ariaLabel={ariaLabel} title={title} table={table}>
      <div className="space-y-3 py-2">
        {variants.map((v) => (
          <div key={v.label} className="space-y-1">
            <div className="flex justify-between text-xs">
              <span className="font-medium">{v.label}</span>
              <span className="text-muted-foreground tabular-nums">
                {v.mean.toFixed(2)}
                {unit} {ciLabel(v.ci)}
              </span>
            </div>
            <div className="relative h-2 rounded-full bg-muted">
              {/* CI band */}
              <div
                className="absolute h-2 rounded-full"
                style={{
                  left: `${pct(v.ci[0])}%`,
                  width: `${pct(v.ci[1]) - pct(v.ci[0])}%`,
                  background: 'color-mix(in oklch, var(--accent-text) 35%, transparent)',
                }}
              />
              {/* mean dot */}
              <div
                className="absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full"
                style={{ left: `${pct(v.mean)}%`, background: 'var(--accent-text)' }}
              />
            </div>
          </div>
        ))}
      </div>
    </ChartFrame>
  )
}
