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
import {
  fromArgs,
  isTerminal,
  useCliCatalog,
  useCliRuns,
  useWorkspace,
  type CliRunDetail,
  type FormValues,
  type Tier,
} from '@/lib/cli'

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
  // "Run again": a previous run's arguments, loaded into that command's form (never auto-run).
  const [rerun, setRerun] = useState<{
    path: string
    values: FormValues
    format: 'json' | 'text'
    note: string
    n: number
  } | null>(null)

  const selectedPath = params.get('cmd')
  const prefixes = useMemo(
    () => (params.get('filter') ?? '').split(',').map((s) => s.trim()).filter(Boolean),
    [params],
  )
  const selected = catalog?.commands.find((c) => c.path === selectedPath) ?? null

  /** Load a finished run's arguments into its command's form, for the user to review and run. */
  const runAgain = (run: CliRunDetail) => {
    const cmd = catalog?.commands.find((c) => c.path === run.command)
    if (!cmd) return
    const secretDropped = cmd.params.some((p) => p.secret && p.name in run.args)
    const when = new Date(run.created_at * 1000).toLocaleString()
    setRerun((prev) => ({
      path: cmd.path,
      values: fromArgs(cmd, run.args),
      format: run.format === 'json' ? 'json' : 'text',
      note:
        `Loaded from the run of ${when}. Review the arguments, then run.` +
        (secretDropped ? ' Secret values are never kept, so enter them again.' : ''),
      // A fresh form every time, even when the same run is loaded twice.
      n: (prev?.n ?? 0) + 1,
    }))
    update({ cmd: cmd.path })
    document.getElementById('cli-runner')?.scrollIntoView?.({ block: 'start', behavior: 'smooth' })
  }

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
              onSelect={(path) => {
                setRerun(null)
                update({ cmd: path })
              }}
              prefixes={prefixes}
              onClearPrefixes={() => update({ filter: null })}
            />
          </aside>

          <div className="min-w-0 space-y-6">
            {selected ? (
              <div id="cli-runner" className="scroll-mt-4">
                <CommandRunner
                  key={`${selected.path}:${rerun?.path === selected.path ? rerun.n : 0}`}
                  command={selected}
                  workspaceFiles={(workspace?.files ?? []).map((f) => f.path)}
                  initialValues={rerun?.path === selected.path ? rerun.values : undefined}
                  initialFormat={rerun?.path === selected.path ? rerun.format : undefined}
                  note={rerun?.path === selected.path ? rerun.note : undefined}
                  onStarted={(id) => {
                    setRunId(id)
                    setTab('output')
                  }}
                />
              </div>
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
                  <RunOutput runId={runId} canDownload={admin} onRerun={runAgain} />
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
                        <span className="text-muted-foreground">{runWhen(r.created_at)}</span>
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

/** When a run started: the time for today's runs, the date and time for older ones (history persists). */
function runWhen(epochSeconds: number): string {
  const at = new Date(epochSeconds * 1000)
  return at.toDateString() === new Date().toDateString() ? at.toLocaleTimeString() : at.toLocaleString()
}
