import { Activity, Server, AlertTriangle, Gauge } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { KpiTile } from '@/components/viz'
import { useSelfObsStatus } from '@/lib/telemetry'

export function SelfObs() {
  const { data, isLoading, error } = useSelfObsStatus()
  const m = data?.metrics

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
            <Activity className="size-6 text-muted-foreground" aria-hidden="true" />
            Dashboard Status
          </h1>
          <p className="text-sm text-muted-foreground">
            The dashboard observing itself — dependency health and request metrics. Auto-refreshes.
          </p>
        </div>
        {data && (
          <StatusPill
            status={data.status === 'up' ? 'ok' : 'warn'}
            label={data.status === 'up' ? 'All systems operational' : 'Degraded'}
          />
        )}
      </div>

      {isLoading && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3" aria-label="Loading status">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-20 w-full" />
          ))}
        </div>
      )}

      {error && (
        <EmptyState
          title="Couldn't load status"
          description="The self-observability endpoint is unreachable."
        />
      )}

      {m && (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <KpiTile label="Requests" value={m.requests} icon={Server} />
            <KpiTile
              label="Server errors"
              value={m.errors}
              icon={AlertTriangle}
              threshold={{ warn: 1, crit: 10 }}
            />
            <KpiTile
              label="Rate-limit hits"
              value={m.rateLimitHits}
              icon={Gauge}
              threshold={{ warn: 1, crit: 20 }}
            />
            <KpiTile label="p95 latency" value={m.latencyMs.p95 ?? 0} unit=" ms" icon={Gauge} />
          </div>

          <section className="space-y-3">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
              Dependencies
            </h2>
            <div className="space-y-2">
              {data.dependencies.map((d) => (
                <div
                  key={d.name}
                  className="flex items-center justify-between rounded-lg border border-border px-4 py-2 text-sm"
                >
                  <span className="font-medium">{d.name}</span>
                  <div className="flex items-center gap-3 text-xs text-muted-foreground">
                    {d.latencyMs !== undefined && <span>{d.latencyMs} ms</span>}
                    <StatusPill
                      status={d.status === 'up' ? 'ok' : d.status === 'degraded' ? 'warn' : 'critical'}
                      label={d.status}
                    />
                  </div>
                </div>
              ))}
            </div>
          </section>
        </>
      )}
    </div>
  )
}
