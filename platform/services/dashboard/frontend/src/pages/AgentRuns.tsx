import { useState } from 'react'
import { Bot } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import {
  useAgentReplay,
  useAgentSessions,
  useAgentTools,
  useBreakerEvents,
} from '@/lib/agentops'

/**
 * Agent runs console (ADR 0021 · Operate group).
 *
 * Read-only replay of Skipper sessions: the recent-session index, one session's ordered tool
 * calls, the per-tool success table and the circuit-breaker's warnings and aborts. Mirrors
 * `exa agentops sessions|replay|tools|anomalies`. Nothing here can start, stop or edit an agent,
 * and tool arguments are never shown - only the redacted digest the recorder stored.
 */
export function AgentRuns() {
  const [selected, setSelected] = useState<string | null>(null)

  return (
    <div className="p-6 space-y-8 max-w-6xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Bot className="size-6 text-muted-foreground" aria-hidden="true" />
          Agent Runs
        </h1>
        <p className="text-sm text-muted-foreground">
          Replay of the Skipper agent&apos;s sessions: which tools it called, what failed, and when
          the circuit-breaker warned or stopped a runaway turn. Read-only.
        </p>
      </div>

      <SessionsSection selected={selected} onSelect={setSelected} />
      {selected && <ReplaySection sessionId={selected} />}
      <ToolsSection />
      <BreakerSection />
    </div>
  )
}

const H2 = 'text-xs font-semibold text-muted-foreground uppercase tracking-widest'

function SessionsSection({ selected, onSelect }: { selected: string | null; onSelect: (id: string) => void }) {
  const { data, isLoading, error } = useAgentSessions()
  return (
    <section className="space-y-3">
      <h2 className={H2}>Recent sessions</h2>
      {error && <EmptyState title="Couldn't load agent sessions" description="The agent-runs endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-24 w-full" />}
      {data && data.length === 0 && (
        <EmptyState title="No agent sessions yet" description="Sessions appear once the agent has answered a turn." />
      )}
      {data && data.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-muted-foreground">
                <th className="py-1 pr-3">Session</th>
                <th className="pr-3">Started</th>
                <th className="pr-3">Status</th>
                <th className="pr-3">Steps</th>
                <th className="pr-3">Errors</th>
                <th className="pr-3">Cost (USD)</th>
                <th>Anomalies</th>
              </tr>
            </thead>
            <tbody>
              {data.map((s) => (
                <tr key={s.session_id} className={selected === s.session_id ? 'bg-muted/40' : undefined}>
                  <td className="py-1 pr-3 font-mono">
                    <button
                      type="button"
                      className="underline-offset-2 hover:underline"
                      aria-label={`Replay ${s.session_id}`}
                      onClick={() => onSelect(s.session_id)}
                    >
                      {s.session_id}
                    </button>
                  </td>
                  <td className="pr-3">{s.started_at}</td>
                  <td className="pr-3">{s.status}</td>
                  <td className="pr-3">{s.steps}</td>
                  <td className="pr-3">{s.errors}</td>
                  <td className="pr-3">{s.cost_usd.toFixed(4)}</td>
                  <td>{s.anomalies.length ? s.anomalies.join(', ') : '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function ReplaySection({ sessionId }: { sessionId: string }) {
  const { data, isLoading, error } = useAgentReplay(sessionId)
  return (
    <section className="space-y-3">
      <h2 className={H2}>Replay: {sessionId}</h2>
      {error && <EmptyState title="Couldn't load this session" description="It may belong to another tenant or no longer exist." />}
      {isLoading && <Skeleton className="h-24 w-full" />}
      {data && (
        <ol className="space-y-1 text-xs">
          {data.steps.map((st, i) => (
            <li key={`${st.step}-${i}`} className="flex flex-wrap gap-x-3 rounded-lg border border-border px-3 py-1.5">
              <span className="text-muted-foreground">#{st.step}</span>
              <span className="font-mono">{st.tool}</span>
              <span>{st.ok ? 'ok' : 'failed'}</span>
              {st.latency_ms != null && <span>{st.latency_ms.toFixed(0)} ms</span>}
              {st.args_digest && <span className="font-mono text-muted-foreground">args {st.args_digest}</span>}
              {st.error && <span className="text-destructive">{st.error}</span>}
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

function ToolsSection() {
  const { data, isLoading, error } = useAgentTools()
  return (
    <section className="space-y-3">
      <h2 className={H2}>Tool success</h2>
      {error && <EmptyState title="Couldn't load tool analytics" description="The agent-runs endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-16 w-full" />}
      {data && data.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3 text-xs">
          {data.map((t) => (
            <div key={t.tool} className="rounded-lg border border-border px-3 py-2">
              <div className="font-mono">{t.tool}</div>
              <div className="mt-0.5 text-muted-foreground">
                {(t.success_rate * 100).toFixed(0)}% of {t.calls} calls succeeded
                {t.avg_latency_ms != null && ` · ${t.avg_latency_ms.toFixed(0)} ms avg`}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function BreakerSection() {
  const { data, isLoading, error } = useBreakerEvents()
  return (
    <section className="space-y-3">
      <h2 className={H2}>Circuit-breaker</h2>
      {error && <EmptyState title="Couldn't load breaker events" description="The agent-runs endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-12 w-full" />}
      {data && data.length === 0 && (
        <p className="text-xs text-muted-foreground">No warnings or aborts recorded.</p>
      )}
      {data && data.length > 0 && (
        <ul className="space-y-1 text-xs">
          {data.map((e, i) => (
            <li key={`${e.ts}-${i}`} className="flex flex-wrap gap-x-3 rounded-lg border border-border px-3 py-1.5">
              <span className="text-muted-foreground">{e.ts}</span>
              <span className={e.event === 'tripped' ? 'font-semibold' : undefined}>{e.event}</span>
              <span className="font-mono">{e.target}</span>
              {e.details.detail && <span className="text-muted-foreground">{e.details.detail}</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
