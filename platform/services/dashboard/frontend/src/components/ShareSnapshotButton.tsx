import { useState } from 'react'
import { useLocation } from 'react-router-dom'
import { Share2, Check } from 'lucide-react'
import { useCreateSnapshot, snapshotShareUrl } from '@/lib/collab'

/**
 * Share the current view as a time-frozen snapshot (F22 / ADR 0073, R2). Creates a scoped, expiring,
 * read-only token server-side and copies the shareable link to the clipboard.
 */
export function ShareSnapshotButton() {
  const { pathname, search } = useLocation()
  const create = useCreateSnapshot()
  const [copied, setCopied] = useState(false)

  const share = () => {
    create.mutate(
      { path: pathname, search, capturedAt: 'now' },
      {
        onSuccess: (res) => {
          navigator.clipboard?.writeText(snapshotShareUrl(res.token)).catch(() => {})
          setCopied(true)
        },
      },
    )
  }

  return (
    <button
      type="button"
      onClick={share}
      disabled={create.isPending}
      aria-label="Share a snapshot of this view"
      className="no-print flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-muted disabled:opacity-40"
    >
      {copied ? <Check className="size-3.5 text-green-500" aria-hidden="true" /> : <Share2 className="size-3.5" aria-hidden="true" />}
      {copied ? 'Link copied' : 'Share snapshot'}
    </button>
  )
}
