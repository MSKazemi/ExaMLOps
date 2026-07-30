import { useState } from 'react'
import { ClipboardCheck, ChevronDown, ChevronRight, Check, X } from 'lucide-react'
import {
  useApprovals,
  useApproveModel,
  useRejectModel,
  type ApprovalEntry,
} from '@/lib/api'
import type { Column } from '@/lib/datagrid'
import { ConsoleView, type RowAction } from '@/components/console'

function StatusBadge({ status }: { status: ApprovalEntry['status'] }) {
  const styles: Record<ApprovalEntry['status'], { bg: string; border: string; color: string; label: string }> = {
    pending: {
      bg: 'oklch(0.78 0.18 55 / 12%)',
      border: 'oklch(0.78 0.18 55 / 30%)',
      color: 'var(--warning-text)',
      label: 'Pending',
    },
    approved: {
      bg: 'oklch(0.72 0.18 155 / 12%)',
      border: 'oklch(0.72 0.18 155 / 30%)',
      color: 'var(--success-text)',
      label: 'Approved',
    },
    rejected: {
      bg: 'oklch(0.66 0.22 25 / 12%)',
      border: 'oklch(0.66 0.22 25 / 25%)',
      color: 'var(--error-text)',
      label: 'Rejected',
    },
  }
  const s = styles[status]
  return (
    <span
      className="px-2 py-0.5 rounded-md text-xs font-medium uppercase tracking-wide"
      style={{ background: s.bg, border: `1px solid ${s.border}`, color: s.color }}
    >
      {s.label}
    </span>
  )
}

function ChangedFiles({ files }: { files: string[] }) {
  const [open, setOpen] = useState(false)
  if (files.length === 0) return <span className="text-muted-foreground text-xs">—</span>
  return (
    <div>
      <button
        onClick={() => setOpen(o => !o)}
        className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        {files.length} file{files.length !== 1 ? 's' : ''}
      </button>
      {open && (
        <ul className="mt-1 space-y-0.5 pl-1">
          {files.map(f => (
            <li key={f} className="font-mono text-[11px] text-muted-foreground truncate max-w-xs" title={f}>
              {f}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/**
 * Approvals — the reference console (ADR 0097 §2): a declarative descriptor (columns + row actions)
 * rendered by the generic `ConsoleView`. The approve/reject mutations still flow through the same
 * `useApproveModel`/`useRejectModel` hooks the page always used (shared code path, no parallel impl).
 */
export function Approvals() {
  const [showAll, setShowAll] = useState(false)
  const { data, isLoading, error } = useApprovals(showAll ? undefined : 'pending')
  const approve = useApproveModel()
  const reject = useRejectModel()

  const columns: Column<ApprovalEntry>[] = [
    {
      key: 'model_id',
      header: 'Model',
      accessor: (r) => r.model_id,
      sortable: true,
      render: (r) => <span className="font-mono text-xs font-semibold">{r.model_id}</span>,
    },
    {
      key: 'commit',
      header: 'Commit',
      accessor: (r) => r.commit_sha,
      render: (r) => (
        <span className="font-mono text-xs text-muted-foreground" title={r.commit_sha}>
          {r.commit_sha.slice(0, 8)}
        </span>
      ),
    },
    {
      key: 'commit_msg',
      header: 'Message',
      accessor: (r) => r.commit_msg,
      render: (r) => (
        <span className="line-clamp-2 max-w-xs inline-block align-top" title={r.commit_msg}>
          {r.commit_msg}
        </span>
      ),
    },
    {
      key: 'files',
      header: 'Changed files',
      accessor: (r) => r.changed_files.length,
      render: (r) => <ChangedFiles files={r.changed_files} />,
    },
    {
      key: 'requested_at',
      header: 'Requested',
      accessor: (r) => r.requested_at,
      sortable: true,
      render: (r) => (
        <span className="text-xs text-muted-foreground whitespace-nowrap">
          {new Date(r.requested_at).toLocaleString()}
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      accessor: (r) => r.status,
      facet: true,
      render: (r) => (
        <div>
          <StatusBadge status={r.status} />
          {r.status === 'rejected' && r.reject_reason && (
            <p className="mt-1 text-[11px] text-muted-foreground italic" title={r.reject_reason}>
              {r.reject_reason}
            </p>
          )}
        </div>
      ),
    },
  ]

  const rowActions: RowAction<ApprovalEntry>[] = [
    {
      id: 'approve',
      label: 'Approve',
      icon: Check,
      variant: 'success',
      visible: (r) => r.status === 'pending',
      run: async (r) => {
        await approve.mutateAsync(r.model_id)
      },
    },
    {
      id: 'reject',
      label: 'Reject',
      icon: X,
      variant: 'danger',
      visible: (r) => r.status === 'pending',
      needsReason: true,
      run: async (r, { reason }) => {
        await reject.mutateAsync({ modelId: r.model_id, reason: reason ?? '' })
      },
    },
  ]

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <ConsoleView<ApprovalEntry>
        title="Approvals"
        subtitle="Review and approve or reject model retrain requests."
        icon={ClipboardCheck}
        columns={columns}
        rows={data}
        getRowId={(r) => r.id}
        rowActions={rowActions}
        isLoading={isLoading}
        error={error}
        errorMessage="Failed to load approvals. Is the Control Plane reachable?"
        emptyTitle={showAll ? 'No approval entries' : 'No pending approvals'}
        emptyDescription={
          showAll
            ? 'No model-change approvals have been recorded yet.'
            : 'Nothing is waiting for review. New retrain requests will appear here.'
        }
        storageKey="approvals"
        toolbar={
          <label className="flex items-center gap-2 text-sm text-muted-foreground cursor-pointer select-none">
            <input
              type="checkbox"
              checked={showAll}
              onChange={(e) => setShowAll(e.target.checked)}
              className="rounded"
            />
            Show all statuses
          </label>
        }
      />
    </div>
  )
}
