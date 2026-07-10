// Reusable data grid — sort, facet-filter, paginate, row-select, bulk actions, CSV export (F17 / ADR 0061).
//
// Dependency-free (no TanStack / virtualization lib). Composes the pure helpers in `lib/datagrid.ts`, so
// every non-trivial transform is unit-tested there and the component stays thin. Density is persisted to
// localStorage under `storageKey`. Bulk actions require an inline confirm — never a silent mass mutation (R4).

import { useMemo, useState } from 'react'
import { ArrowDown, ArrowUp, ArrowUpDown, Download, X } from 'lucide-react'
import { applySort, computeFacets, toCsv, toggleSelection, type Column, type SortSpec } from '@/lib/datagrid'

export interface BulkAction<T> {
  label: string
  /** Runs against the selected rows; may be async. Confirmed inline before firing (R4). */
  onRun: (rows: T[]) => void | Promise<void>
  destructive?: boolean
}

interface DataGridProps<T> {
  columns: Column<T>[]
  rows: T[]
  getRowId: (row: T) => string
  initialSort?: SortSpec[]
  pageSize?: number
  bulkActions?: BulkAction<T>[]
  /** localStorage key for density persistence (R1). */
  storageKey?: string
  /** Accessible label for the export button / table. */
  label?: string
}

function readDensity(key?: string): 'comfortable' | 'compact' {
  if (!key || typeof localStorage === 'undefined') return 'comfortable'
  return localStorage.getItem(`datagrid.${key}.density`) === 'compact' ? 'compact' : 'comfortable'
}

function downloadCsv(name: string, csv: string): void {
  if (typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') return
  const blob = new Blob([csv], { type: 'text/csv' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${name}.csv`
  a.click()
  URL.revokeObjectURL(url)
}

export function DataGrid<T>({
  columns,
  rows,
  getRowId,
  initialSort,
  pageSize = 25,
  bulkActions = [],
  storageKey,
  label = 'data',
}: DataGridProps<T>) {
  const [sort, setSort] = useState<SortSpec[]>(initialSort ?? [])
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const [density, setDensity] = useState<'comfortable' | 'compact'>(() => readDensity(storageKey))
  const [pendingBulk, setPendingBulk] = useState<number | null>(null)

  // filter → facets(on filtered) → sort; pagination is derived at render (clamped) to avoid effect syncing.
  const filtered = useMemo(
    () =>
      rows.filter((row) =>
        Object.entries(filters).every(([col, val]) => {
          if (!val) return true
          const acc = columns.find((c) => c.key === col)?.accessor
          return acc ? String(acc(row)) === val : true
        }),
      ),
    [rows, filters, columns],
  )
  const facets = useMemo(() => computeFacets(filtered, columns), [filtered, columns])
  const sorted = useMemo(() => applySort(filtered, columns, sort), [filtered, columns, sort])

  const pageCount = Math.max(1, Math.ceil(sorted.length / pageSize))
  const clampedPage = Math.min(page, pageCount - 1)
  const pageRows = sorted.slice(clampedPage * pageSize, clampedPage * pageSize + pageSize)

  const padY = density === 'compact' ? 'py-1' : 'py-2'

  function cycleSort(key: string) {
    setPage(0)
    setSort((prev) => {
      const cur = prev.find((s) => s.col === key)
      if (!cur) return [{ col: key, dir: 'asc' }]
      if (cur.dir === 'asc') return [{ col: key, dir: 'desc' }]
      return [] // third click clears
    })
  }

  function toggleFacet(col: string, value: string) {
    setPage(0)
    setFilters((prev) => (prev[col] === value ? { ...prev, [col]: '' } : { ...prev, [col]: value }))
  }

  function toggleDensity() {
    setDensity((prev) => {
      const next = prev === 'compact' ? 'comfortable' : 'compact'
      if (storageKey && typeof localStorage !== 'undefined')
        localStorage.setItem(`datagrid.${storageKey}.density`, next)
      return next
    })
  }

  const selectedRows = sorted.filter((r) => selected.has(getRowId(r)))
  const facetColumns = columns.filter((c) => c.facet)

  async function runBulk(idx: number) {
    const action = bulkActions[idx]
    if (!action) return
    await action.onRun(selectedRows)
    setSelected(new Set())
    setPendingBulk(null)
  }

  return (
    <div className="space-y-3">
      {/* Toolbar: facet chips + density + export */}
      <div className="flex flex-wrap items-center gap-2">
        {facetColumns.map((col) => (
          <div key={col.key} className="flex flex-wrap items-center gap-1">
            <span className="text-xs uppercase tracking-wider text-muted-foreground">{col.header}:</span>
            {Object.entries(facets[col.key] ?? {}).map(([value, count]) => {
              const active = filters[col.key] === value
              return (
                <button
                  key={value}
                  type="button"
                  onClick={() => toggleFacet(col.key, value)}
                  aria-pressed={active}
                  className={`rounded-full border px-2 py-0.5 text-xs ${
                    active ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted-foreground'
                  }`}
                >
                  {value} ({count})
                </button>
              )
            })}
          </div>
        ))}
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={toggleDensity}
            className="rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-muted"
          >
            {density === 'compact' ? 'Comfortable' : 'Compact'}
          </button>
          <button
            type="button"
            onClick={() => downloadCsv(label, toCsv(sorted, columns))}
            aria-label={`Export ${label} as CSV`}
            className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-muted"
          >
            <Download className="size-3" aria-hidden="true" />
            Export CSV
          </button>
        </div>
      </div>

      {/* Bulk-action bar (R4) */}
      {bulkActions.length > 0 && selected.size > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-primary/40 bg-primary/5 px-3 py-2 text-sm">
          <span className="font-medium">{selected.size} selected</span>
          {pendingBulk === null ? (
            <>
              {bulkActions.map((a, i) => (
                <button
                  key={a.label}
                  type="button"
                  onClick={() => setPendingBulk(i)}
                  className={`rounded-md px-2 py-1 text-xs ${
                    a.destructive
                      ? 'border border-red-500/50 text-red-600 dark:text-red-400'
                      : 'border border-border text-foreground'
                  }`}
                >
                  {a.label}
                </button>
              ))}
              <button
                type="button"
                onClick={() => setSelected(new Set())}
                aria-label="Clear selection"
                className="ml-auto text-muted-foreground hover:text-foreground"
              >
                <X className="size-4" aria-hidden="true" />
              </button>
            </>
          ) : (
            <>
              <span className="text-muted-foreground">
                Confirm &ldquo;{bulkActions[pendingBulk]?.label}&rdquo; on {selected.size} row(s)?
              </span>
              <button
                type="button"
                onClick={() => runBulk(pendingBulk)}
                className="rounded-md border border-primary bg-primary px-2 py-1 text-xs text-primary-foreground"
              >
                Confirm
              </button>
              <button
                type="button"
                onClick={() => setPendingBulk(null)}
                className="rounded-md border border-border px-2 py-1 text-xs"
              >
                Cancel
              </button>
            </>
          )}
        </div>
      )}

      {/* Grid */}
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-sm" aria-label={label}>
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted-foreground">
              {bulkActions.length > 0 && <th className={`px-3 ${padY} w-8`} />}
              {columns.map((col) => {
                const dir = sort.find((s) => s.col === col.key)?.dir
                return (
                  <th key={col.key} className={`px-3 ${padY} font-medium`}>
                    {col.sortable ? (
                      <button
                        type="button"
                        onClick={() => cycleSort(col.key)}
                        className="flex items-center gap-1 hover:text-foreground"
                        aria-label={`Sort by ${col.header}`}
                      >
                        {col.header}
                        {dir === 'asc' ? (
                          <ArrowUp className="size-3" aria-hidden="true" />
                        ) : dir === 'desc' ? (
                          <ArrowDown className="size-3" aria-hidden="true" />
                        ) : (
                          <ArrowUpDown className="size-3 opacity-40" aria-hidden="true" />
                        )}
                      </button>
                    ) : (
                      col.header
                    )}
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {pageRows.map((row) => {
              const id = getRowId(row)
              return (
                <tr key={id} className="border-b border-border/50">
                  {bulkActions.length > 0 && (
                    <td className={`px-3 ${padY}`}>
                      <input
                        type="checkbox"
                        checked={selected.has(id)}
                        onChange={() => setSelected((prev) => toggleSelection(prev, id))}
                        aria-label={`Select row ${id}`}
                      />
                    </td>
                  )}
                  {columns.map((col) => (
                    <td key={col.key} className={`px-3 ${padY} tabular-nums`}>
                      {col.render ? col.render(row) : col.accessor(row)}
                    </td>
                  ))}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* Footer: count + pagination */}
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          {sorted.length} row{sorted.length === 1 ? '' : 's'}
          {selected.size > 0 && ` · ${selected.size} selected`}
        </span>
        {pageCount > 1 && (
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setPage(Math.max(0, clampedPage - 1))}
              disabled={clampedPage === 0}
              className="rounded border border-border px-2 py-0.5 disabled:opacity-40"
            >
              Prev
            </button>
            <span>
              {clampedPage + 1} / {pageCount}
            </span>
            <button
              type="button"
              onClick={() => setPage(Math.min(pageCount - 1, clampedPage + 1))}
              disabled={clampedPage >= pageCount - 1}
              className="rounded border border-border px-2 py-0.5 disabled:opacity-40"
            >
              Next
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
