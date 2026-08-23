# Data Grid & Bulk Operations

The dashboard ships a single reusable table primitive — `<DataGrid/>` — that every list surface uses
instead of hand-rolling a `<table>`. It provides sorting, faceted filtering, pagination, row selection,
audited bulk actions, and CSV export, all **dependency-free** (no TanStack Table / virtualization lib).

- **Feature:** F17 · **Design:** ADR 0061 (`design/adr/0061-dashboard-data-grid-bulk-ops.md`) ·
  **Spec:** `design/vision/specs/F17-data-grid-bulk-ops.md`
- **Frontend:** `platform/services/dashboard/frontend/src/lib/datagrid.ts` (pure helpers) +
  `components/DataGrid.tsx` (component)
- **First consumer:** the FinOps **cost-by-model** table (`pages/Finops.tsx`)

## Using the grid

```tsx
import { DataGrid } from '@/components/DataGrid'
import type { Column } from '@/lib/datagrid'

const COLUMNS: Column<Row>[] = [
  { key: 'model', header: 'Model', accessor: (r) => r.model, sortable: true },
  { key: 'env', header: 'Env', accessor: (r) => r.env, facet: true },      // facet → filter chips
  { key: 'cost', header: 'Cost', accessor: (r) => r.cost,                  // numeric → sorts numerically
    render: (r) => usd(r.cost), sortable: true },                          // render → shown formatted
]

<DataGrid
  columns={COLUMNS}
  rows={rows}
  getRowId={(r) => r.id}
  initialSort={[{ col: 'cost', dir: 'desc' }]}
  storageKey="finops-cost"     // persists density preference
  label="cost-by-model"        // used for the a11y label + CSV filename
  bulkActions={[{ label: 'Delete', destructive: true, onRun: (rows) => api.delete(rows) }]}
/>
```

### `Column<T>` fields

| Field | Purpose |
|---|---|
| `accessor` | Scalar used for **sort / filter / facet / CSV export** (and display, unless `render` is set) |
| `render` | Optional cell renderer for **display only** — keeps numeric sort correct on a formatted column |
| `sortable` | Enables the sortable header (click cycles asc → desc → unsorted) |
| `facet` | Marks a facet dimension — renders filter chips with live value counts |

## The query envelope (R2)

The pure layer models a list query and its result:

```ts
ListQuery  { sort?: SortSpec[]; filters?: Record<string,string>; page?: number; pageSize?: number }
ListResult { rows: T[]; total: number; pageCount: number; facets: Record<string, Record<string, number>> }
```

`queryRows(rows, columns, query)` runs the full client-side pipeline: **filter → facets (over filtered)
→ sort → paginate**. This is deliberately the same shape a BFF list endpoint should return, so a grid
can migrate from client-side to server-side execution **without changing the API contract**.

`encodeQuery`/`decodeQuery` round-trip a `ListQuery` through URL search params (`?sort=cost:desc&f.env=prod&page=1`),
so a filtered/sorted view is shareable and deep-linkable.

## Bulk actions are never silent (R4)

When rows are selected a bulk-action bar appears. Clicking an action does **not** fire it — it shows an
inline **Confirm / Cancel** prompt first. On confirm, the action's `onRun(selectedRows)` runs against the
selected rows (it may be async), then the selection clears. Backends that mutate should authorize and
audit server-side; the grid guarantees no accidental mass mutation from the UI.

## Export (R6)

The **Export CSV** control serializes the *current filtered + sorted* view (all pages, not just the
visible page) via `toCsv`, which RFC-4180-escapes commas/quotes/newlines. The download filename is the
`label`.

## Notes & limits

This slice ships the client-side grid (sort/filter/facet/paginate/select/bulk/export) and its first
adoption. Deferred (tracked in the plan): row **virtualization** for very large sets, **server-side**
query execution against a BFF endpoint, **saved views** (R5), column show/hide + reorder, and Parquet
export.

See [`docs/dashboard/architecture.md`](../dashboard/architecture.md#data-grid--bulk-operations-f17) for
the design diagram.
