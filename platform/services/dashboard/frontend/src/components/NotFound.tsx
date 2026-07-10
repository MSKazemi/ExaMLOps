import { Link } from 'react-router-dom'
import { FileQuestion } from 'lucide-react'
import { EmptyState } from '@/components/ui/empty-state'
import { buttonVariants } from '@/components/ui/button'

export interface NotFoundProps {
  /** Optional entity kind for a specific message, e.g. "Model" -> "Model not found". */
  entity?: string
  /** Where the "back" action links (defaults to the overview). */
  homeHref?: string
}

/**
 * NotFound — the designed 404 surface (ADR 0050 / F1). Used as a route element and for unknown entities.
 */
export function NotFound({ entity, homeHref = '/' }: NotFoundProps) {
  return (
    <div className="flex min-h-[60vh] items-center justify-center p-6">
      <EmptyState
        icon={FileQuestion}
        title={entity ? `${entity} not found` : 'Page not found'}
        description="The page or resource you're looking for doesn't exist or may have been moved."
        action={
          <Link to={homeHref} className={buttonVariants({ variant: 'default' })}>
            Back to overview
          </Link>
        }
      />
    </div>
  )
}
