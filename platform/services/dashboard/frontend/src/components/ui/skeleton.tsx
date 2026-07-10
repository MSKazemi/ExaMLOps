import { type ComponentProps } from 'react'
import { cn } from '@/lib/utils'

/**
 * Skeleton — a content-shaped loading placeholder (ADR 0051 / F3).
 *
 * The standard async loading convention across the dashboard, replacing spinners. Size and shape it
 * with utility classes (e.g. `h-4 w-24`) so the placeholder matches the content it stands in for.
 */
export function Skeleton({ className, ...props }: ComponentProps<'div'>) {
  return (
    <div
      data-slot="skeleton"
      className={cn('animate-pulse rounded-md bg-muted', className)}
      {...props}
    />
  )
}
