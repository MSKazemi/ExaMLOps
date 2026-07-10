import { ChartFrame } from './ChartFrame'
import { histogram } from '@/lib/viz'

export interface DistributionProps {
  values: number[]
  bins?: number
  title?: string
  ariaLabel?: string
  height?: number
}

/**
 * Distribution — a themed histogram (F4 R5 / ADR 0055) with the required data-table fallback.
 *
 * Dependency-free SVG bars sized to the container; each bin's count is also exposed in the
 * `<ChartFrame>` data table for screen readers (F4 R7).
 */
export function Distribution({
  values,
  bins = 10,
  title,
  ariaLabel = 'Value distribution histogram',
  height = 120,
}: DistributionProps) {
  const buckets = histogram(values, bins)
  const maxCount = buckets.reduce((m, b) => Math.max(m, b.count), 0) || 1
  const barW = buckets.length ? 100 / buckets.length : 0

  const table = {
    columns: [
      { key: 'range', label: 'Range' },
      { key: 'count', label: 'Count' },
    ],
    rows: buckets.map((b) => ({
      range: `${b.x0.toFixed(2)} – ${b.x1.toFixed(2)}`,
      count: b.count,
    })),
  }

  return (
    <ChartFrame ariaLabel={ariaLabel} title={title} table={table}>
      {buckets.length === 0 ? (
        <p className="text-sm text-muted-foreground py-8 text-center">No data</p>
      ) : (
        <svg
          viewBox={`0 0 100 ${height}`}
          preserveAspectRatio="none"
          className="w-full"
          style={{ height }}
          aria-hidden="true"
        >
          {buckets.map((b, i) => {
            const h = (b.count / maxCount) * (height - 2)
            return (
              <rect
                key={i}
                x={i * barW + barW * 0.1}
                y={height - h}
                width={barW * 0.8}
                height={h}
                fill="var(--accent-text)"
                opacity={0.75}
              />
            )
          })}
        </svg>
      )}
    </ChartFrame>
  )
}
