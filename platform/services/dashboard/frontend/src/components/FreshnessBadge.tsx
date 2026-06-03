export interface FreshnessBadgeProps {
  status: 'current' | 'stale' | 'unknown'
  staleSince?: string | null
}

export function FreshnessBadge({ status, staleSince }: FreshnessBadgeProps) {
  if (status === 'current') {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200">
        CURRENT
      </span>
    )
  }
  if (status === 'stale') {
    const title = staleSince ? `ModelZoo updated since ${staleSince}` : 'ModelZoo has new commits'
    return (
      <span
        title={title}
        className="inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200"
      >
        UPDATED
      </span>
    )
  }
  return null
}
