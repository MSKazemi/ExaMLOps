/**
 * Generic list-console renderer (dashboard-enterprise-rebuild M0, BL-013b · ADR 0097 §2).
 *
 * Standardizes the header / loading / error / empty / grid layout so every domain console looks and
 * behaves identically, and appends a capability-gated actions column driven by the descriptor's
 * `rowActions`. Bespoke React is reserved for genuinely visual surfaces; everything list-shaped goes
 * through here. `Approvals` is the reference port.
 */
import { useState } from 'react'
import type { Column } from '@/lib/datagrid'
import { DataGrid } from '@/components/DataGrid'
import { EmptyState } from '@/components/ui/empty-state'
import { Skeleton } from '@/components/ui/skeleton'
import { useCapabilities } from '@/lib/capabilities'
import { cn } from '@/lib/utils'
import type { ConsoleViewProps, RowAction } from './types'

const VARIANT_STYLE: Record<string, React.CSSProperties> = {
  success: {
    background: 'oklch(0.72 0.18 155 / 15%)',
    border: '1px solid oklch(0.72 0.18 155 / 35%)',
    color: 'var(--success-text)',
  },
  danger: {
    background: 'oklch(0.66 0.22 25 / 12%)',
    border: '1px solid oklch(0.66 0.22 25 / 28%)',
    color: 'var(--error-text)',
  },
  default: {
    background: 'var(--surface-1)',
    border: '1px solid var(--border)',
    color: 'var(--text-2)',
  },
}

/** Per-row action controls — capability gate, optional reason input, optional confirm, busy state. */
function ActionCell<T>({ row, actions }: { row: T; actions: RowAction<T>[] }) {
  const { capabilities, can, reason } = useCapabilities()
  const [openReasonFor, setOpenReasonFor] = useState<string | null>(null)
  const [confirmFor, setConfirmFor] = useState<string | null>(null)
  const [reasonText, setReasonText] = useState('')
  const [runningId, setRunningId] = useState<string | null>(null)

  const visible = actions.filter((a) => (a.visible ? a.visible(row) : true))
  if (visible.length === 0) return <span className="text-muted-foreground text-xs">—</span>

  const doRun = async (action: RowAction<T>, reasonArg?: string) => {
    setRunningId(action.id)
    try {
      await action.run(row, { reason: reasonArg })
      setOpenReasonFor(null)
      setConfirmFor(null)
      setReasonText('')
    } finally {
      setRunningId(null)
    }
  }

  const onClick = (action: RowAction<T>) => {
    if (action.needsReason) {
      setOpenReasonFor((cur) => (cur === action.id ? null : action.id))
      setConfirmFor(null)
      return
    }
    if (action.confirm) {
      setConfirmFor((cur) => (cur === action.id ? null : action.id))
      return
    }
    void doRun(action)
  }

  const busy = runningId !== null

  return (
    <div className="flex flex-col gap-1.5 min-w-[160px]">
      <div className="flex flex-wrap items-center gap-1.5">
        {visible.map((action) => {
          const denied = action.capability ? !can(action.capability) : false
          const Icon = action.icon
          return (
            <button
              key={action.id}
              type="button"
              onClick={() => onClick(action)}
              disabled={denied || busy}
              aria-label={action.label}
              title={denied ? reason(action.capability!) || 'Not permitted' : undefined}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium transition-all disabled:opacity-50"
              style={VARIANT_STYLE[action.variant ?? 'default']}
            >
              {Icon && <Icon className="w-3 h-3" />} {action.label}
            </button>
          )
        })}
      </div>

      {/* Inline reason input (needsReason). Confirm is disabled until a non-empty reason is given. */}
      {openReasonFor && (() => {
        const action = visible.find((a) => a.id === openReasonFor)
        if (!action) return null
        return (
          <div className="flex items-center gap-1.5">
            <input
              type="text"
              value={reasonText}
              onChange={(e) => setReasonText(e.target.value)}
              placeholder="Reason…"
              aria-label={`${action.label} reason`}
              className="flex-1 text-xs rounded-md px-2 py-1 bg-transparent outline-none"
              style={{ border: '1px solid var(--border)', color: 'var(--foreground)' }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && reasonText.trim()) void doRun(action, reasonText)
              }}
              autoFocus
            />
            <button
              type="button"
              onClick={() => void doRun(action, reasonText)}
              disabled={!reasonText.trim() || busy}
              className="px-2 py-1 rounded-md text-xs font-medium transition-all disabled:opacity-40"
              style={{ ...VARIANT_STYLE[action.variant ?? 'default'] }}
            >
              Confirm
            </button>
          </div>
        )
      })()}

      {/* Inline confirm step (confirm, no reason). */}
      {confirmFor && (() => {
        const action = visible.find((a) => a.id === confirmFor)
        if (!action) return null
        return (
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span>Confirm “{action.label}”?</span>
            <button
              type="button"
              onClick={() => void doRun(action)}
              disabled={busy}
              className="px-2 py-1 rounded-md font-medium disabled:opacity-40"
              style={VARIANT_STYLE[action.variant ?? 'default']}
            >
              Yes
            </button>
            <button
              type="button"
              onClick={() => setConfirmFor(null)}
              className="px-2 py-1 rounded-md"
              style={VARIANT_STYLE.default}
            >
              Cancel
            </button>
          </div>
        )
      })()}

      {/* Note when the role lacks the capability for every action (honest, not hidden). */}
      {visible.length > 0 &&
        visible.every((a) => a.capability && !can(a.capability)) &&
        capabilities.length >= 0 && (
          <span className="text-[10px] text-muted-foreground italic">Read-only for your role</span>
        )}
    </div>
  )
}

export function ConsoleView<T>(props: ConsoleViewProps<T>) {
  const {
    title,
    subtitle,
    icon: Icon,
    columns,
    rows,
    getRowId,
    rowActions,
    storageKey,
    emptyTitle,
    emptyDescription,
    isLoading,
    error,
    errorMessage,
    toolbar,
  } = props

  const gridColumns: Column<T>[] =
    rowActions && rowActions.length
      ? [
          ...columns,
          {
            key: '__actions',
            header: 'Actions',
            sortable: false,
            accessor: () => '',
            render: (row: T) => <ActionCell row={row} actions={rowActions} />,
          },
        ]
      : columns

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div
            className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
            style={{ background: 'oklch(0.64 0.20 265 / 12%)', border: '1px solid oklch(0.64 0.20 265 / 30%)' }}
          >
            <Icon className="w-4 h-4" style={{ color: 'var(--accent-text)' }} />
          </div>
          <div>
            <h1 className="text-2xl font-bold">{title}</h1>
            {subtitle && <p className="text-muted-foreground text-sm mt-1">{subtitle}</p>}
          </div>
        </div>
        {toolbar}
      </div>

      {Boolean(error) && (
        <p
          className="text-sm rounded-lg px-4 py-3"
          style={{
            background: 'oklch(0.66 0.22 25 / 12%)',
            border: '1px solid oklch(0.66 0.22 25 / 25%)',
            color: 'var(--error-text)',
          }}
        >
          {errorMessage ?? 'Failed to load data.'}
        </p>
      )}

      {isLoading && !rows && (
        <div className="space-y-2 rounded-xl p-4" style={{ border: '1px solid var(--border)' }} aria-label={`Loading ${title}`}>
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className={cn('h-9 w-full')} />
          ))}
        </div>
      )}

      {rows && rows.length === 0 && (
        <EmptyState icon={Icon} title={emptyTitle ?? 'Nothing here yet'} description={emptyDescription} />
      )}

      {rows && rows.length > 0 && (
        <DataGrid<T>
          columns={gridColumns}
          rows={rows}
          getRowId={getRowId}
          storageKey={storageKey}
          label={title}
        />
      )}
    </div>
  )
}
