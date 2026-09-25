import { Bot, Calculator, FlaskConical } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { useLlmops, passRateLabel, evalTone, providerOriginLabel, type EvalModel } from '@/lib/llmops'

function EvalCard({ e }: { e: EvalModel }) {
  return (
    <div className="rounded-lg border border-border p-4 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">
          {e.model} <span className="text-xs text-muted-foreground">· {e.suite}</span>
        </span>
        <StatusPill status={evalTone(e.passRate)} label={passRateLabel(e.passRate)} />
      </div>
      {e.metrics.length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          {e.metrics.map((m) => (
            <span key={m.metric} className="tabular-nums">
              {m.metric}: {m.value}
              {m.baseline !== null && <span className="opacity-60"> / {m.baseline}</span>}{' '}
              <StatusPill status={m.passed ? 'ok' : 'critical'} label={m.passed ? 'pass' : 'fail'} showIcon={false} />
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

export function Llmops() {
  const { data, isLoading, error } = useLlmops()
  const endpoints = data?.endpoints
  const evals = data?.evals
  const calculations = data?.calculations
  const partial = data?._partial ?? []

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Bot className="size-6 text-muted-foreground" aria-hidden="true" />
          LLMOps
        </h1>
        <p className="text-sm text-muted-foreground">
          LLM endpoint registry and continuous-eval scores — sourced from the platform&apos;s LLM-serving substrate.
        </p>
      </div>

      {partial.length > 0 && (
        <StatusPill status="warn" label={`Partial data — ${partial.join(', ')} unavailable`} />
      )}

      {isLoading && (
        <div className="space-y-2" aria-label="Loading LLMOps">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
      )}

      {error && (
        <EmptyState title="Couldn't load LLMOps data" description="The LLMOps endpoint is unreachable." />
      )}

      {data && (
        <>
          <section className="space-y-3">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
              Endpoint registry
            </h2>
            {endpoints && endpoints.rows.length > 0 ? (
              <div className="overflow-x-auto rounded-lg border border-border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
                      <th className="px-3 py-2 font-medium">Model</th>
                      <th className="px-3 py-2 font-medium">Engine</th>
                      <th className="px-3 py-2 font-medium">HF model</th>
                      <th className="px-3 py-2 font-medium">TP</th>
                      <th className="px-3 py-2 font-medium">dtype</th>
                      <th className="px-3 py-2 font-medium">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {endpoints.rows.map((e) => (
                      <tr key={e.model} className="border-b border-border/50">
                        <td className="px-3 py-2 font-medium">{e.model}</td>
                        <td className="px-3 py-2 text-muted-foreground">{e.engine}</td>
                        <td className="px-3 py-2 text-muted-foreground font-mono text-xs">{e.hfModelId}</td>
                        <td className="px-3 py-2 text-muted-foreground tabular-nums">{e.tensorParallel}</td>
                        <td className="px-3 py-2 text-muted-foreground">{e.dtype}</td>
                        <td className="px-3 py-2">
                          <StatusPill status={e.enabled ? 'ok' : 'warn'} label={e.enabled ? 'Enabled' : 'Disabled'} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title="No LLM endpoints" description="Register endpoints in llm_endpoints." />
            )}
          </section>

          <section className="space-y-3">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest flex items-center gap-1.5">
              <FlaskConical className="size-3.5" /> Continuous eval
            </h2>
            {evals && evals.models.length > 0 ? (
              <div className="space-y-2">
                {evals.models.map((e) => (
                  <EvalCard key={e.model} e={e} />
                ))}
              </div>
            ) : (
              <EmptyState title="No eval runs yet" description="Run an eval suite to see scores." />
            )}
          </section>

          {calculations && calculations.available && calculations.rows.length > 0 && (
            <section className="space-y-3">
              <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest flex items-center gap-1.5">
                <Calculator className="size-3.5" /> How the numbers are computed
              </h2>
              <div className="space-y-2">
                {calculations.rows.map((p) => (
                  <div key={p.domain} className="rounded-lg border border-border p-3 space-y-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-medium">
                        {p.domain}{' '}
                        <span className="text-xs text-muted-foreground font-mono">
                          · {p.provider ?? 'built-in arithmetic'}
                          {p.version ? ` v${p.version}` : ''}
                        </span>
                      </span>
                      <StatusPill
                        status={p.ok ? (p.selected ? 'ok' : 'unknown') : 'critical'}
                        label={providerOriginLabel(p)}
                        showIcon={false}
                      />
                    </div>
                    <p className="text-xs text-muted-foreground">{p.ok ? p.methodology : p.error}</p>
                  </div>
                ))}
              </div>
            </section>
          )}

          <p className="text-xs text-muted-foreground border-l-2 border-border pl-3">
            Prompt studio, gateway routing, semantic cache, RAG-ops, and vector-DB views are not yet
            available — they will appear here as their backends are wired.
          </p>
        </>
      )}
    </div>
  )
}
