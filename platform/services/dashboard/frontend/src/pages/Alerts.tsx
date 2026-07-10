import { BellRing } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { useAlerts, useAckAlert, severityToken, inboxHeadline, type Alert } from '@/lib/alerts'

function AlertRow({ a }: { a: Alert }) {
  const ack = useAckAlert()
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-border px-4 py-2">
      <div className="flex items-center gap-3 min-w-0">
        <StatusPill status={severityToken(a.severity)} label={a.severity} />
        <span className="text-sm truncate">{a.title}</span>
        <span className="text-[10px] uppercase tracking-wide text-muted-foreground shrink-0">{a.source}</span>
      </div>
      <button
        type="button"
        onClick={() => ack.mutate(a.id)}
        disabled={ack.isPending}
        className="shrink-0 rounded-md border border-border px-2.5 py-1 text-xs hover:bg-muted disabled:opacity-50"
      >
        {ack.isPending ? 'Acking…' : 'Ack'}
      </button>
    </div>
  )
}

export function Alerts() {
  const { data, isLoading, error } = useAlerts()
  const inbox = data?.inbox
  const partial = data?._partial ?? []

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
            <BellRing className="size-6 text-muted-foreground" aria-hidden="true" />
            Alerts
          </h1>
          <p className="text-sm text-muted-foreground">
            Unified alert inbox — drift, budget, and eval regressions. Acknowledging is audited.
          </p>
        </div>
        {inbox && (
          <StatusPill
            status={inbox.count === 0 ? 'ok' : (inbox.counts.critical || inbox.counts.error) ? 'critical' : 'warn'}
            label={inboxHeadline(inbox.counts)}
          />
        )}
      </div>

      {partial.length > 0 && (
        <StatusPill status="warn" label={`Partial data — ${partial.join(', ')} unavailable`} />
      )}

      {isLoading && (
        <div className="space-y-2" aria-label="Loading alerts">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
      )}

      {error && <EmptyState title="Couldn't load alerts" description="The alerts endpoint is unreachable." />}

      {inbox && inbox.alerts.length === 0 && !isLoading && (
        <EmptyState title="All clear" description="No active alerts across drift, budget, or eval." />
      )}

      {inbox && inbox.alerts.length > 0 && (
        <div className="space-y-2">
          {inbox.alerts.map((a) => (
            <AlertRow key={a.id} a={a} />
          ))}
        </div>
      )}
    </div>
  )
}
