// Shared data-grid framework — pure, dependency-free helpers (F17 / ADR 0061).
//
// The <DataGrid/> component composes these; the sort/filter/facet/paginate/export logic lives here so
// it is unit-testable and reusable. The query envelope (`ListQuery` → `ListResult`) matches the shape
// BFF list endpoints should serve (R2), so a grid can move from client-side to server-side without an
// API-shape change.

export interface Column<T> {
  key: string
  header: string
  /** Scalar used for sorting / filtering / faceting / CSV export (and display unless `render` is set). */
  accessor: (row: T) => string | number
  /** Optional cell renderer for display only — sorting/filtering/export still use `accessor`. */
  render?: (row: T) => import('react').ReactNode
  sortable?: boolean
  /** Mark as a facet dimension (drives filter chips + counts, R3). */
  facet?: boolean
}

export interface SortSpec {
  col: string
  dir: 'asc' | 'desc'
}

export interface ListQuery {
  sort?: SortSpec[]
  filters?: Record<string, string>
  page?: number
  pageSize?: number
}

export interface ListResult<T> {
  rows: T[]
  total: number
  pageCount: number
  facets: Record<string, Record<string, number>>
}

// ── sort / filter / facet / paginate (R2/R3) ──────────────────────────────────

function accessorFor<T>(columns: Column<T>[], key: string): ((row: T) => string | number) | null {
  return columns.find((c) => c.key === key)?.accessor ?? null
}

/** Stable multi-column sort (does not mutate the input). */
export function applySort<T>(rows: T[], columns: Column<T>[], sort: SortSpec[] | undefined): T[] {
  if (!sort || sort.length === 0) return rows
  const decorated = rows.map((row, i) => ({ row, i }))
  decorated.sort((a, b) => {
    for (const s of sort) {
      const acc = accessorFor(columns, s.col)
      if (!acc) continue
      const av = acc(a.row)
      const bv = acc(b.row)
      if (av < bv) return s.dir === 'asc' ? -1 : 1
      if (av > bv) return s.dir === 'asc' ? 1 : -1
    }
    return a.i - b.i // stable
  })
  return decorated.map((d) => d.row)
}

/** Case-insensitive substring filter per column (empty filter → all rows). */
export function applyFilter<T>(rows: T[], columns: Column<T>[], filters: Record<string, string> | undefined): T[] {
  if (!filters) return rows
  const active = Object.entries(filters).filter(([, v]) => v !== '' && v != null)
  if (active.length === 0) return rows
  return rows.filter((row) =>
    active.every(([col, needle]) => {
      const acc = accessorFor(columns, col)
      if (!acc) return true
      return String(acc(row)).toLowerCase().includes(String(needle).toLowerCase())
    }),
  )
}

/** Facet value counts for facet columns, computed over the given (already-filtered) rows (R3). */
export function computeFacets<T>(rows: T[], columns: Column<T>[]): Record<string, Record<string, number>> {
  const out: Record<string, Record<string, number>> = {}
  for (const col of columns.filter((c) => c.facet)) {
    const counts: Record<string, number> = {}
    for (const row of rows) {
      const v = String(col.accessor(row))
      counts[v] = (counts[v] ?? 0) + 1
    }
    out[col.key] = counts
  }
  return out
}

/** Full client-side query: filter → facets(on filtered) → sort → paginate (R2). */
export function queryRows<T>(rows: T[], columns: Column<T>[], query: ListQuery): ListResult<T> {
  const filtered = applyFilter(rows, columns, query.filters)
  const facets = computeFacets(filtered, columns)
  const sorted = applySort(filtered, columns, query.sort)
  const pageSize = query.pageSize ?? 25
  const page = query.page ?? 0
  const pageCount = Math.max(1, Math.ceil(sorted.length / pageSize))
  const start = page * pageSize
  return { rows: sorted.slice(start, start + pageSize), total: filtered.length, pageCount, facets }
}

// ── selection (R4) ────────────────────────────────────────────────────────────

/** Toggle an id in a selection set, returning a new set (immutable). */
export function toggleSelection(selected: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(selected)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  return next
}

// ── export (R6) ───────────────────────────────────────────────────────────────

function csvCell(value: string | number): string {
  const s = String(value)
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

/** Serialize rows to CSV using the columns' headers + accessors (R6). */
export function toCsv<T>(rows: T[], columns: Column<T>[]): string {
  const header = columns.map((c) => csvCell(c.header)).join(',')
  const body = rows.map((row) => columns.map((c) => csvCell(c.accessor(row))).join(','))
  return [header, ...body].join('\n')
}

// ── URL query encoding (R3 shareable) ──────────────────────────────────────────

/** Encode a list query into URL search params (sort + filters), shareable/deep-linkable. */
export function encodeQuery(query: ListQuery): string {
  const params = new URLSearchParams()
  if (query.sort?.length) params.set('sort', query.sort.map((s) => `${s.col}:${s.dir}`).join(','))
  for (const [k, v] of Object.entries(query.filters ?? {})) {
    if (v) params.set(`f.${k}`, v)
  }
  if (query.page) params.set('page', String(query.page))
  return params.toString()
}

/** Decode URL search params back into a list query. */
export function decodeQuery(search: string): ListQuery {
  const params = new URLSearchParams(search)
  const sort: SortSpec[] = (params.get('sort') ?? '')
    .split(',')
    .filter(Boolean)
    .map((tok) => {
      const [col, dir] = tok.split(':')
      return { col, dir: dir === 'desc' ? 'desc' : 'asc' }
    })
  const filters: Record<string, string> = {}
  for (const [k, v] of params.entries()) {
    if (k.startsWith('f.')) filters[k.slice(2)] = v
  }
  const pageRaw = params.get('page')
  return {
    sort: sort.length ? sort : undefined,
    filters: Object.keys(filters).length ? filters : undefined,
    page: pageRaw ? Number(pageRaw) : undefined,
  }
}
