import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Search, Table2 } from 'lucide-react'
import { EmptyState } from '@/components/ui/empty-state'
import { Skeleton } from '@/components/ui/skeleton'
import { ResourceView } from '@/components/cli/ResourceView'
import { CAP, useCapabilities } from '@/lib/capabilities'
import { CLI_CONSOLE_PATH, useCliCatalog, useWorkspace, type CliCommand } from '@/lib/cli'
import { groupResources } from '@/lib/resources'
import { fuzzyScore } from '@/lib/search'
import { cn } from '@/lib/utils'

/**
 * Resource Manager (ADR 0119 · Platform group) — everything `exa` manages, as tables.
 *
 * Projects, connections, workbenches, prompts, SLOs, keys, clusters, secrets, backups… each one a
 * table built from its list command, with New / View / Edit / Delete and every other action on a
 * row. Declared once in the platform (`examlops.cli.resources`), executed through the CLI Console,
 * so a new resource needs no new page and every action keeps the CLI's tiers and audit trail.
 */
export function Resources() {
  const caps = useCapabilities()
  const [params, setParams] = useSearchParams()
  const { data: catalog, isLoading, error } = useCliCatalog()
  const { data: workspace } = useWorkspace(caps.can(CAP.CLI_WRITE))
  const [query, setQuery] = useState('')

  const resources = useMemo(() => catalog?.resources ?? [], [catalog])
  const commands = useMemo(
    () => Object.fromEntries((catalog?.commands ?? []).map((c) => [c.path, c])) as Record<string, CliCommand>,
    [catalog],
  )
  const selectedId = params.get('r') ?? resources[0]?.id ?? null
  const selected = resources.find((r) => r.id === selectedId) ?? null

  const visible = useMemo(() => {
    const q = query.trim()
    if (!q) return resources
    return resources.filter((r) => Math.max(fuzzyScore(q, r.title), fuzzyScore(q, r.id), fuzzyScore(q, r.list)) > 0)
  }, [resources, query])
  const grouped = groupResources(visible, catalog?.panels ?? [])
  const cov = catalog?.resource_coverage

  return (
    <div className="mx-auto flex h-full max-w-7xl flex-col gap-4 p-6">
      <div className="space-y-1">
        <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
          <Table2 className="size-6 text-muted-foreground" aria-hidden="true" />
          Resources
        </h1>
        <p className="text-sm text-muted-foreground">
          Everything the platform manages, as tables — create, view, edit, delete and every other action per item,
          each one the same <code className="text-xs">exa</code> command the terminal runs, audited.
        </p>
        {cov && (
          <p className="text-[11px] text-muted-foreground" aria-label="Resource coverage">
            <strong className="text-foreground">{cov.resources}</strong> resources ·{' '}
            <strong className="text-foreground">{cov.commands}</strong> commands as table actions · the other{' '}
            {cov.runnable - cov.commands} (reports, checks, one-off operations) are in the{' '}
            <Link to={CLI_CONSOLE_PATH} className="text-primary hover:underline">
              CLI Console
            </Link>
          </p>
        )}
      </div>

      {error && <EmptyState title="Couldn't load the resource catalog" description="The CLI Console endpoint is unreachable, or the platform package is not installed in this deployment." />}
      {isLoading && <Skeleton className="h-96 w-full" />}

      {catalog && (
        <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[15rem_1fr]">
          <aside className="flex min-h-[16rem] flex-col gap-2 rounded-lg border border-border p-2 lg:h-[calc(100vh-12rem)]">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-2 size-3.5 text-muted-foreground" aria-hidden="true" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={`Search ${resources.length} resources…`}
                aria-label="Search resources"
                className="w-full rounded-md border border-border bg-transparent py-1.5 pl-7 pr-2 text-xs outline-none focus:border-primary"
              />
            </div>
            <nav aria-label="Resources" className="min-h-0 flex-1 overflow-y-auto">
              {grouped.map(([panel, items]) => (
                <div key={panel} className="mb-2">
                  <p className="px-1 py-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">{panel}</p>
                  <ul className="space-y-px">
                    {items.map((r) => (
                      <li key={r.id}>
                        <button
                          type="button"
                          onClick={() => setParams({ r: r.id }, { replace: true })}
                          aria-current={selected?.id === r.id ? 'true' : undefined}
                          className={cn(
                            'w-full rounded-md px-2 py-1 text-left text-xs hover:bg-muted',
                            selected?.id === r.id && 'bg-muted font-medium',
                          )}
                        >
                          {r.title}
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
              {grouped.length === 0 && <p className="px-2 py-4 text-xs text-muted-foreground">No resource matches.</p>}
            </nav>
          </aside>

          <div className="min-w-0">
            {selected && commands[selected.list] ? (
              <ResourceView
                key={selected.id}
                resource={selected}
                commands={commands}
                workspaceFiles={(workspace?.files ?? []).map((f) => f.path)}
              />
            ) : (
              <EmptyState icon={Table2} title="Pick a resource" />
            )}
          </div>
        </div>
      )}
    </div>
  )
}
