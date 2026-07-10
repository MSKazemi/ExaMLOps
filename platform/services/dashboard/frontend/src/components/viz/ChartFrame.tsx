import { type ReactNode } from 'react'

export interface ChartTableColumn {
  key: string
  label: string
}

export interface ChartFrameProps {
  /** Accessible description of what the chart shows (required, F4 R7 / F18). */
  ariaLabel: string
  /** Optional visible heading. */
  title?: string
  /** Columns + rows for the always-present data-table fallback (F4 R7). */
  table: { columns: ChartTableColumn[]; rows: Record<string, string | number>[] }
  className?: string
  children: ReactNode
}

/**
 * ChartFrame — the required accessibility wrapper for every F4 chart (ADR 0055 R7 / F18).
 *
 * It labels the visual with an aria description and always ships a screen-reader- and
 * keyboard-reachable **data-table fallback** in a `<details>` disclosure, so no chart is
 * information that only exists as pixels. Chart components render their SVG as `children`.
 */
export function ChartFrame({ ariaLabel, title, table, className, children }: ChartFrameProps) {
  return (
    <figure className={className} aria-label={ariaLabel} role="group">
      {title && (
        <figcaption className="text-xs font-semibold text-muted-foreground uppercase tracking-widest mb-2">
          {title}
        </figcaption>
      )}
      <div role="img" aria-label={ariaLabel}>
        {children}
      </div>
      <details className="mt-2">
        <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
          Data table
        </summary>
        <div className="overflow-x-auto mt-1">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-muted-foreground border-b border-border">
                {table.columns.map((c) => (
                  <th key={c.key} className="px-2 py-1 font-medium">
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((row, i) => (
                <tr key={i} className="border-b border-border/40">
                  {table.columns.map((c) => (
                    <td key={c.key} className="px-2 py-1 tabular-nums">
                      {row[c.key]}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  )
}
