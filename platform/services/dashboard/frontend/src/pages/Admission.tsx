import { useState, type ReactNode } from 'react'
import { ListChecks } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { isAdmin } from '@/lib/auth'
import { useAdmission, useSubmitAdmission } from '@/lib/admission'

/**
 * Admission console (Phase 1 item 1.5, dashboard-enterprise-rebuild · Operate group).
 *
 * Surfaces `exa admission` in the UI over pure `platform.db` state — the durable, per-tenant
 * fair-share admission-control queue. Viewers see queue depth by state (via the shared
 * `examlops.admission.stats` path); admins enqueue a work item through the shared
 * `examlops.admission.submit` path (audited `source=dashboard`). Viewers are read-only — the write
 * controls render disabled with an explanation (F15 R3). No live drain / actuation here.
 */
export function Admission() {
  const admin = isAdmin()

  return (
    <div className="p-6 space-y-8 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <ListChecks className="size-6 text-muted-foreground" aria-hidden="true" />
          Admission Queue
        </h1>
        <p className="text-sm text-muted-foreground">
          Durable, per-tenant fair-share admission control between triggers and the pipeline engine.
          Queue depth and enqueue only — the worker drains under the global + per-tenant caps.
        </p>
        {!admin && (
          <p className="text-xs text-muted-foreground">Requires the admin role to submit work.</p>
        )}
      </div>

      <QueueSection />
      <SubmitSection admin={admin} />
    </div>
  )
}

function QueueSection() {
  const { data, isLoading, error } = useAdmission()
  const stats = data?.stats

  return (
    <section className="space-y-3">
      <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Queue depth</h2>

      {error && <EmptyState title="Couldn't load the admission queue" description="The admission endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-20 w-full" />}

      {stats && (
        <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-5 text-xs">
          <Stat label="Queued" value={stats.queued} />
          <Stat label="Running" value={stats.running} />
          <Stat label="Done" value={stats.done} />
          <Stat label="Rejected" value={stats.rejected} />
          <Stat label="Failed" value={stats.failed} />
        </div>
      )}
    </section>
  )
}

function SubmitSection({ admin }: { admin: boolean }) {
  const submitAdmission = useSubmitAdmission()
  const [kind, setKind] = useState('retrain')
  const [tenant, setTenant] = useState('default')
  const [priority, setPriority] = useState('0')
  const [payload, setPayload] = useState('{}')
  const [msg, setMsg] = useState<string | null>(null)

  const submit = async () => {
    setMsg(null)
    if (!kind.trim()) {
      setMsg('Kind is required.')
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
      const r = await submitAdmission.mutateAsync({
        kind: kind.trim(),
        tenant: tenant.trim() || 'default',
        priority: Number(priority) || 0,
        payload: payload.trim() || '{}',
      })
      setMsg(`Queued admission #${r.id} (${r.kind}, tenant=${r.tenant}).`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to submit work item')
    }
  }

  return (
    <section className="space-y-3">
      <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Submit work</h2>

      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-muted-foreground">
          Kind
          <input aria-label="Work kind" value={kind} onChange={(e) => setKind(e.target.value)}
            disabled={!admin} placeholder="retrain | pipeline"
            className="mt-1 block w-40 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50" />
        </label>
        <label className="text-xs text-muted-foreground">
          Tenant
          <input aria-label="Tenant" value={tenant} onChange={(e) => setTenant(e.target.value)}
            disabled={!admin}
            className="mt-1 block w-32 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50" />
        </label>
        <label className="text-xs text-muted-foreground">
          Priority
          <input aria-label="Priority" type="number" value={priority} onChange={(e) => setPriority(e.target.value)}
            disabled={!admin}
            className="mt-1 block w-24 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50" />
        </label>
        <label className="text-xs text-muted-foreground">
          Payload (JSON)
          <input aria-label="Payload JSON" value={payload} onChange={(e) => setPayload(e.target.value)}
            disabled={!admin}
            className="mt-1 block w-56 rounded-md border border-border bg-transparent px-2 py-1 text-xs font-mono disabled:opacity-50" />
        </label>
        <button onClick={submit} disabled={!admin || submitAdmission.isPending}
          title={admin ? undefined : 'Requires the admin role.'}
          className="rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50">
          Submit
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
