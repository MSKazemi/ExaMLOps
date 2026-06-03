import { useState } from 'react'
import { ClipboardCheck, ChevronDown, ChevronRight, Check, X } from 'lucide-react'
import {
  useApprovals,
  useApproveModel,
  useRejectModel,
  type ApprovalEntry,
} from '@/lib/api'

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

function ApprovalRow({ entry }: { entry: ApprovalEntry }) {
  const [rejectOpen, setRejectOpen] = useState(false)
  const [rejectReason, setRejectReason] = useState('')
  const approve = useApproveModel()
  const reject = useRejectModel()
  const isPending = entry.status === 'pending'
  const isBusy = approve.isPending || reject.isPending

  const handleApprove = () => {
    approve.mutate(entry.model_id)
  }

  const handleRejectConfirm = () => {
    reject.mutate(
      { modelId: entry.model_id, reason: rejectReason },
      {
        onSuccess: () => {
          setRejectOpen(false)
          setRejectReason('')
        },
      },
    )
  }

  return (
    <tr className="border-t" style={{ borderColor: 'var(--border-sm)' }}>
      <td className="p-3 font-mono text-xs font-semibold">{entry.model_id}</td>
      <td className="p-3 font-mono text-xs text-muted-foreground" title={entry.commit_sha}>
        {entry.commit_sha.slice(0, 8)}
      </td>
      <td className="p-3 text-sm max-w-xs">
        <span className="line-clamp-2" title={entry.commit_msg}>{entry.commit_msg}</span>
      </td>
      <td className="p-3">
        <ChangedFiles files={entry.changed_files} />
      </td>
      <td className="p-3 text-xs text-muted-foreground whitespace-nowrap">
        {new Date(entry.requested_at).toLocaleString()}
      </td>
      <td className="p-3">
        <StatusBadge status={entry.status} />
        {entry.status === 'rejected' && entry.reject_reason && (
          <p className="mt-1 text-[11px] text-muted-foreground italic" title={entry.reject_reason}>
            {entry.reject_reason}
          </p>
        )}
      </td>
      <td className="p-3">
        {isPending && (
          <div className="flex flex-col gap-1.5 min-w-[160px]">
            <div className="flex items-center gap-1.5">
              <button
                onClick={handleApprove}
                disabled={isBusy}
                className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium transition-all disabled:opacity-50"
                style={{
                  background: 'oklch(0.72 0.18 155 / 15%)',
                  border: '1px solid oklch(0.72 0.18 155 / 35%)',
                  color: 'var(--success-text)',
                }}
              >
                <Check className="w-3 h-3" /> Approve
              </button>
              <button
                onClick={() => setRejectOpen(o => !o)}
                disabled={isBusy}
                className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium transition-all disabled:opacity-50"
                style={{
                  background: 'oklch(0.66 0.22 25 / 12%)',
                  border: '1px solid oklch(0.66 0.22 25 / 28%)',
                  color: 'var(--error-text)',
                }}
              >
                <X className="w-3 h-3" /> Reject
              </button>
            </div>
            {rejectOpen && (
              <div className="flex items-center gap-1.5">
                <input
                  type="text"
                  value={rejectReason}
                  onChange={e => setRejectReason(e.target.value)}
                  placeholder="Reason…"
                  className="flex-1 text-xs rounded-md px-2 py-1 bg-transparent outline-none"
                  style={{ border: '1px solid var(--border)', color: 'var(--foreground)' }}
                  onKeyDown={e => { if (e.key === 'Enter' && rejectReason.trim()) handleRejectConfirm() }}
                  autoFocus
                />
                <button
                  onClick={handleRejectConfirm}
                  disabled={!rejectReason.trim() || isBusy}
                  className="px-2 py-1 rounded-md text-xs font-medium transition-all disabled:opacity-40"
                  style={{
                    background: 'oklch(0.66 0.22 25 / 20%)',
                    border: '1px solid oklch(0.66 0.22 25 / 35%)',
                    color: 'var(--error-text)',
                  }}
                >
                  Confirm
                </button>
              </div>
            )}
          </div>
        )}
      </td>
    </tr>
  )
}

export function Approvals() {
  const [showAll, setShowAll] = useState(false)
  const statusFilter = showAll ? undefined : 'pending'
  const { data, isLoading, error } = useApprovals(statusFilter)

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div
            className="w-9 h-9 rounded-lg flex items-center justify-center"
            style={{
              background: 'oklch(0.78 0.18 55 / 12%)',
              border: '1px solid oklch(0.78 0.18 55 / 30%)',
            }}
          >
            <ClipboardCheck className="w-4 h-4" style={{ color: 'var(--warning-text)' }} />
          </div>
          <div>
            <h1 className="text-2xl font-bold">Approvals</h1>
            <p className="text-muted-foreground text-sm mt-1">
              Review and approve or reject model retrain requests.
            </p>
          </div>
        </div>

        <label className="flex items-center gap-2 text-sm text-muted-foreground cursor-pointer select-none">
          <input
            type="checkbox"
            checked={showAll}
            onChange={e => setShowAll(e.target.checked)}
            className="rounded"
          />
          Show all statuses
        </label>
      </div>

      {error && (
        <p
          className="text-sm rounded-lg px-4 py-3"
          style={{
            background: 'oklch(0.66 0.22 25 / 12%)',
            border: '1px solid oklch(0.66 0.22 25 / 25%)',
            color: 'var(--error-text)',
          }}
        >
          Failed to load approvals. Is the Control Plane reachable?
        </p>
      )}

      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}

      {data && (
        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
          <table className="w-full text-sm">
            <thead style={{ background: 'var(--surface-1)' }}>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="p-3">Model</th>
                <th className="p-3">Commit</th>
                <th className="p-3">Message</th>
                <th className="p-3">Changed files</th>
                <th className="p-3">Requested</th>
                <th className="p-3">Status</th>
                <th className="p-3">Actions</th>
              </tr>
            </thead>
            <tbody style={{ background: 'var(--surface-0)' }}>
              {data.length === 0 ? (
                <tr>
                  <td colSpan={7} className="p-6 text-center text-muted-foreground">
                    {showAll ? 'No approval entries found.' : 'No pending approvals.'}
                  </td>
                </tr>
              ) : (
                data.map(entry => <ApprovalRow key={entry.id} entry={entry} />)
              )}
            </tbody>
          </table>
          <div
            className="p-3 text-xs text-muted-foreground text-right"
            style={{ background: 'var(--surface-1)' }}
          >
            {data.length} {showAll ? 'total' : 'pending'}
          </div>
        </div>
      )}
    </div>
  )
}
