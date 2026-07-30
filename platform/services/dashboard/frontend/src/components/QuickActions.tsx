import { Link } from 'react-router-dom'
import { Zap } from 'lucide-react'
import { getRole } from '@/lib/auth'
import { visibleCommands } from '@/lib/commands'

/**
 * Home command-center quick-actions strip (F1). A curated set of primary operator destinations in
 * lifecycle order, rendered from the shared command registry so labels + role-scoping stay consistent
 * with the ⌘K palette and the sidebar (admin-only targets automatically drop out for viewers).
 */
const QUICK_PATHS = [
  '/build/models',
  '/build/pipelines',
  '/operate/drift',
  '/operate/slos',
  '/serve/gateway',
  '/operate/autopilot',
  '/govern/approvals',
]

export function QuickActions() {
  const role = getRole()
  const byPath = new Map(visibleCommands(role).filter((c) => c.to).map((c) => [c.to as string, c]))
  const actions = QUICK_PATHS.map((p) => byPath.get(p)).filter((c): c is NonNullable<typeof c> => Boolean(c))
  if (actions.length === 0) return null
  return (
    <div className="space-y-2">
      <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Quick actions</h2>
      <div className="flex flex-wrap gap-2">
        {actions.map((a) => (
          <Link
            key={a.to}
            to={a.to as string}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs font-medium hover:bg-accent/60 transition-colors"
            style={{ background: 'var(--surface-1)' }}
          >
            <Zap className="size-3 opacity-70" aria-hidden="true" /> {a.label.replace(/^Go to /, '')}
          </Link>
        ))}
      </div>
    </div>
  )
}
