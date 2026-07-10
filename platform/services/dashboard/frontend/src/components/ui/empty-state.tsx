import { type ReactNode } from 'react'
import { Inbox, type LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface EmptyStateProps {
  /** Illustrative icon (defaults to an inbox). */
  icon?: LucideIcon
  /** Short, specific heading, e.g. "No models yet". */
  title: string
  /** Optional supporting sentence explaining what to do next. */
  description?: string
  /** Optional call-to-action (button/link) — turns a dead end into a next step. */
  action?: ReactNode
  className?: string
}

/**
 * EmptyState — the designed "nothing here yet" surface (ADR 0050 / F1).
 *
 * Every list/detail surface renders this instead of a blank area, ideally with a call-to-action.
 * Exposed as an accessible region labelled by its title.
 */
export function EmptyState({ icon: Icon = Inbox, title, description, action, className }: EmptyStateProps) {
  return (
    <div
      role="region"
      aria-label={title}
      className={cn(
        'flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border px-6 py-12 text-center',
        className,
      )}
    >
      <div className="rounded-full bg-muted p-3 text-muted-foreground">
        <Icon aria-hidden="true" className="size-6" />
      </div>
      <div className="space-y-1">
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        {description && <p className="mx-auto max-w-sm text-sm text-muted-foreground">{description}</p>}
      </div>
      {action && <div className="mt-1">{action}</div>}
    </div>
  )
}
