import type { ReactNode } from 'react'
import { SquareTerminal, type LucideIcon } from 'lucide-react'
import { EmptyState } from '@/components/ui/empty-state'
import { useFlag } from '@/lib/serverflags'

interface Props {
  children: ReactNode
  /** An embedded surface (a section, a link) that simply disappears when the switch is off. */
  quiet?: boolean
  /** The page's own title and icon, so a switched-off page still reads as that page. */
  title?: string
  icon?: LucideIcon
}

/**
 * Renders its children only while the `cliConsole` feature flag is on (ADR 0119 · F25).
 *
 * The backend is what enforces the switch — off, every `/api/v1/cli/*` call answers 403 — so this
 * is presentation: a page says plainly that the console is switched off, inside the same page frame
 * and heading it normally has, instead of showing a wall of failed requests; embedded surfaces
 * (`quiet`) simply disappear.
 */
export function CliFlagGate({ children, quiet = false, title = 'CLI Console', icon: Icon = SquareTerminal }: Props) {
  const on = useFlag('cliConsole')
  if (on) return <>{children}</>
  if (quiet) return null
  return (
    <div className="mx-auto flex h-full max-w-7xl flex-col gap-4 p-6">
      <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
        <Icon className="size-6 text-muted-foreground" aria-hidden="true" />
        {title}
      </h1>
      <EmptyState
        icon={SquareTerminal}
        title="The CLI Console is switched off"
        description="An administrator has turned off the cliConsole feature flag, so this dashboard does not run exa commands. An admin can turn it back on under Platform → Flags."
      />
    </div>
  )
}
