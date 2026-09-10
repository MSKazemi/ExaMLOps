import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { SquareTerminal } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { CommandBrowser } from '@/components/cli/CommandBrowser'
import { CommandRunner } from '@/components/cli/CommandRunner'
import { RunOutput } from '@/components/cli/RunOutput'
import { TierBadge } from '@/components/cli/TierBadge'
import { WorkspacePanel } from '@/components/cli/WorkspacePanel'
import { CAP, useCapabilities } from '@/lib/capabilities'
import { StatusPill } from '@/components/ui/status-pill'
import { isTerminal, useCliCatalog, useCliRuns, useWorkspace, type Tier } from '@/lib/cli'

const TIERS: Tier[] = ['read', 'admin', 'destructive', 'cli_only']

/**
 * CLI Console (ADR 0119 · Platform group) — every `exa` command, in the dashboard.
 *
 * The bespoke consoles are the rich UI for the hot paths; this page is the guarantee that nothing
 * the CLI can do is missing from the dashboard. It lists the live command tree (built from the same
 * code the terminal runs), renders a form for each command's parameters, and runs the real CLI
 * server-side. What a user may run is decided by the platform's own per-command tiers — viewers run
 * reads, admins run writes, destructive commands need the command typed back — and every run lands
 * in the audit chain.
 */
export function CliConsole() {
  const caps = useCapabilities()
  const admin = caps.can(CAP.CLI_WRITE)
  const [params, setParams] = useSearchParams()
  const { data: catalog, isLoading, error } = useCliCatalog()
  const { data: runs } = useCliRuns()
  const { data: workspace } = useWorkspace(admin)
  const [runId, setRunId] = useState<string | null>(null)
  const [tab, setTab] = useState<'output' | 'history' | 'workspace'>('output')

  const selectedPath = params.get('cmd')
  const prefixes = useMemo(
    () => (params.get('filter') ?? '').split(',').map((s) => s.trim()).filter(Boolean),
    [params],
  )
  const selected = catalog?.commands.find((c) => c.path === selectedPath) ?? null

  const update = (next: Record<string, string | null>) => {
    const p = new URLSearchParams(params)
    for (const [k, v] of Object.entries(next)) {
      if (v === null) p.delete(k)
      else p.set(k, v)
    }
    setParams(p, { replace: true })
  }

  return (
    <div className="mx-auto flex h-full max-w-7xl flex-col gap-4 p-6">
      <div className="space-y-1">
        <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
          <SquareTerminal className="size-6 text-muted-foreground" aria-hidden="true" />
          CLI Console
        </h1>
        <p className="text-sm text-muted-foreground">
          Every <code className="text-xs">exa</code> command, run from the dashboard through the same code the terminal
          runs. Arguments are checked against each command&rsquo;s own declaration; every run is audited.
        </p>
        {catalog && (
          <p className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground" aria-label="Command coverage">
            <span>
              <strong className="text-foreground">{catalog.total}</strong> commands ·{' '}
              <strong className="text-foreground">{catalog.total - (catalog.tiers.cli_only ?? 0)}</strong> runnable here
            </span>
            {TIERS.map((t) => (
              <span key={t} className="inline-flex items-center gap-1">
                <TierBadge tier={t} /> {catalog.tiers[t] ?? 0}
              </span>
            ))}
          </p>
        )}
      </div>

      {error && <EmptyState title="Couldn't load the command catalog" description="The CLI Console endpoint is unreachable, or the platform package is not installed in this deployment." />}
      {isLoading && <Skeleton className="h-96 w-full" />}

      {catalog && (
        <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[18rem_1fr]">
          <aside className="min-h-[20rem] rounded-lg border border-border p-2 lg:h-[calc(100vh-12rem)]">
            <CommandBrowser
              catalog={catalog}
              selected={selectedPath}
              onSelect={(path) => update({ cmd: path })}
              prefixes={prefixes}
              onClearPrefixes={() => update({ filter: null })}
            />
          </aside>

          <div className="min-w-0 space-y-6">
            {selected ? (
              <CommandRunner
                key={selected.path}
                command={selected}
                workspaceFiles={(workspace?.files ?? []).map((f) => f.path)}
                onStarted={(id) => {
                  setRunId(id)
                  setTab('output')
                }}
              />
            ) : (
              <EmptyState
                icon={SquareTerminal}
                title="Pick a command"
                description="Browse by lifecycle panel or search on the left. Each command shows its help, a form for its parameters and the equivalent terminal line."
              />
            )}

            <div>
              <div role="tablist" aria-label="Runs" className="mb-2 flex gap-1 border-b border-border text-xs">
                {(['output', 'history', ...(admin ? ['workspace'] : [])] as const).map((t) => (
                  <button
                    key={t}
                    type="button"
                    role="tab"
                    aria-selected={tab === t}
                    onClick={() => setTab(t as typeof tab)}
                    className={`-mb-px border-b-2 px-3 py-1.5 capitalize ${tab === t ? 'border-primary font-medium' : 'border-transparent text-muted-foreground'}`}
                  >
                    {t}
                  </button>
                ))}
              </div>
              {tab === 'output' &&
                (runId ? (
                  <RunOutput runId={runId} canDownload={admin} />
                ) : (
                  <p className="text-xs text-muted-foreground">Run a command to see its output here.</p>
                ))}
              {tab === 'history' && (
                <ul className="divide-y divide-border rounded-md border border-border text-xs">
                  {(runs?.runs ?? []).length === 0 && <li className="px-2 py-2 text-muted-foreground">No runs yet.</li>}
                  {(runs?.runs ?? []).map((r) => (
                    <li key={r.id}>
                      <button
                        type="button"
                        onClick={() => {
                          setRunId(r.id)
                          setTab('output')
                          update({ cmd: r.command })
                        }}
                        className="flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-muted"
                      >
                        <StatusPill
                          status={r.status === 'succeeded' ? 'healthy' : isTerminal(r.status) ? (r.status === 'cancelled' ? 'unknown' : 'failed') : 'pending'}
                          label={r.status}
                        />
                        <code className="flex-1 truncate">{r.display}</code>
                        {admin && <span className="text-muted-foreground">{r.actor.replace(/^dashboard:/, '')}</span>}
                        <span className="text-muted-foreground">{new Date(r.created_at * 1000).toLocaleTimeString()}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {tab === 'workspace' && admin && <WorkspacePanel />}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
