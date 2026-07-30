import { useState, type ReactNode } from 'react'
import { Radio } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { isAdmin } from '@/lib/auth'
import { useEvents, usePublishEvent } from '@/lib/events'

/**
 * Events console (Phase 1 item 1.3, NovaFabric event backbone · Platform group).
 *
 * Surfaces `exa events` in the UI over pure `platform.db` state — the transactional-outbox event
 * backbone. Viewers see the outbox backlog by state (via the shared
 * `examlops.data.events.outbox_stats` path); admins enqueue an event through the shared
 * `examlops.events.publish` path (audited `source=dashboard`). Viewers are read-only — the write
 * controls render disabled with an explanation (F15 R3). No live broker relay here.
 */
export function Events() {
  const admin = isAdmin()

  return (
    <div className="p-6 space-y-8 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Radio className="size-6 text-muted-foreground" aria-hidden="true" />
          Event Backbone
        </h1>
        <p className="text-sm text-muted-foreground">
          NovaFabric transactional outbox — drift / promotion / retrain / inference events are
          enqueued once and durably relayed to the broker. Backlog and enqueue only — the relay
          drains out-of-band.
        </p>
        {!admin && (
          <p className="text-xs text-muted-foreground">Requires the admin role to publish events.</p>
        )}
      </div>

      <BacklogSection />
      <PublishSection admin={admin} />
    </div>
  )
}

function BacklogSection() {
  const { data, isLoading, error } = useEvents()
  const stats = data?.stats

  return (
    <section className="space-y-3">
      <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Outbox backlog</h2>

      {error && <EmptyState title="Couldn't load the event outbox" description="The events endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-20 w-full" />}

      {stats && (
        <div className="grid gap-2 sm:grid-cols-3 text-xs">
          <Stat label="Pending" value={stats.pending} />
          <Stat label="Published" value={stats.published} />
          <Stat label="Poison" value={stats.poison} />
        </div>
      )}
    </section>
  )
}

function PublishSection({ admin }: { admin: boolean }) {
  const publishEvent = usePublishEvent()
  const [topic, setTopic] = useState('drift.detected')
  const [payload, setPayload] = useState('{}')
  const [msg, setMsg] = useState<string | null>(null)

  const submit = async () => {
    setMsg(null)
    if (!topic.trim()) {
      setMsg('Topic is required.')
      return
    }
    if (payload.trim()) {
      try {
        JSON.parse(payload)
      } catch {
        setMsg('Payload must be valid JSON.')
        return
      }
    }
    try {
      const r = await publishEvent.mutateAsync({
        topic: topic.trim(),
        payload: payload.trim() || '{}',
      })
      setMsg(`Enqueued event #${r.id} on '${r.topic}'.`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to publish event')
    }
  }

  return (
    <section className="space-y-3">
      <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Publish event</h2>

      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-muted-foreground">
          Topic
          <input aria-label="Event topic" value={topic} onChange={(e) => setTopic(e.target.value)}
            disabled={!admin} placeholder="drift.detected"
            className="mt-1 block w-48 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50" />
        </label>
        <label className="text-xs text-muted-foreground">
          Payload (JSON)
          <input aria-label="Payload JSON" value={payload} onChange={(e) => setPayload(e.target.value)}
            disabled={!admin}
            className="mt-1 block w-56 rounded-md border border-border bg-transparent px-2 py-1 text-xs font-mono disabled:opacity-50" />
        </label>
        <button onClick={submit} disabled={!admin || publishEvent.isPending}
          title={admin ? undefined : 'Requires the admin role.'}
          className="rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50">
          Publish
        </button>
      </div>

      {msg && (
        <p className="text-xs rounded-lg px-3 py-2" style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-2)' }}>
          {msg}
        </p>
      )}
    </section>
  )
}

function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-lg border border-border px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-sm">{value}</div>
    </div>
  )
}
