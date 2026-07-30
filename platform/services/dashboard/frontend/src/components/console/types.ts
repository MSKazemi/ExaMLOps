/**
 * Console meta-framework types (dashboard-enterprise-rebuild M0, BL-013b · ADR 0097 §2).
 *
 * Most of the ~55 CLI domains are the same shape — list → row actions. Rather than hand-roll ~55
 * bespoke pages, a console is described declaratively (columns + capability-gated row actions on the
 * shared `DataGrid`) and rendered by one generic `ConsoleView`. Genuinely visual surfaces (DAGs,
 * topology) stay bespoke. Every action inherits the same safety rails uniformly: capability gate,
 * optional confirm, optional reason — and the mutation itself flows through the page's existing
 * `examlops.*`-backed hooks (the Phase-42 shared-code-path rule), never a parallel implementation.
 */
import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import type { Column } from '@/lib/datagrid'

export type ActionVariant = 'default' | 'success' | 'danger'

export interface RowAction<T> {
  id: string
  label: string
  icon?: LucideIcon
  variant?: ActionVariant
  /**
   * Capability required to run (F15). When set and the current role lacks it, the action renders
   * disabled with the deny reason as a tooltip — shown-but-disabled, never hidden, so the UI stays
   * discoverable and honest about why an action is unavailable.
   */
  capability?: string
  /** Render the action only when this predicate holds for the row (e.g. only pending rows). */
  visible?: (row: T) => boolean
  /** Collect a non-empty free-text reason (inline) before running; passed to `run` as `ctx.reason`. */
  needsReason?: boolean
  /** Require an explicit inline confirm click before running (destructive / outward actions). */
  confirm?: boolean
  /** Perform the mutation. May be async; the cell shows a busy state until it settles. */
  run: (row: T, ctx: { reason?: string }) => void | Promise<void>
}

/** The declarative description of a list console — the "descriptor" ADR 0097 §2 refers to. */
export interface ConsoleDescriptor<T> {
  title: string
  subtitle?: string
  icon: LucideIcon
  columns: Column<T>[]
  getRowId: (row: T) => string
  rowActions?: RowAction<T>[]
  /** DataGrid density-persistence key (per-console). */
  storageKey?: string
  emptyTitle?: string
  emptyDescription?: string
}

/** Descriptor + the dynamic data/state the owning page supplies at render time. */
export interface ConsoleViewProps<T> extends ConsoleDescriptor<T> {
  rows: T[] | undefined
  isLoading?: boolean
  error?: unknown
  errorMessage?: string
  /** Console-level controls (filters, toggles) rendered in the header, right-aligned. */
  toolbar?: ReactNode
}
