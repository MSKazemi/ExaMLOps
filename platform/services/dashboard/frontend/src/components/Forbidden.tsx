import { Link } from 'react-router-dom'
import { ShieldAlert } from 'lucide-react'
import { EmptyState } from '@/components/ui/empty-state'
import { buttonVariants } from '@/components/ui/button'

export interface ForbiddenProps {
  /** What was being accessed, e.g. "this tenant's models" — used to explain the denial (F15). */
  resource?: string
  /** Full override for the explanation. Takes precedence over `resource`. */
  reason?: string
  /** Where the "back" action links (defaults to the overview). */
  homeHref?: string
}

/**
 * Forbidden — the designed "no permission" surface (ADR 0050 / F1, ADR 0057 / F15).
 *
 * Never a silent dead end: it always explains why access is denied, satisfying F15 R3
 * ("hide/disable unauthorized actions and explain why").
 */
export function Forbidden({ resource, reason, homeHref = '/' }: ForbiddenProps) {
  const description =
    reason ??
    (resource
      ? `You don't have permission to view ${resource}.`
      : "You don't have permission to view this resource.")

  return (
    <div className="flex min-h-[60vh] items-center justify-center p-6">
      <EmptyState
        icon={ShieldAlert}
        title="Access denied"
        description={description}
        action={
          <Link to={homeHref} className={buttonVariants({ variant: 'outline' })}>
            Back to overview
          </Link>
        }
      />
    </div>
  )
}
