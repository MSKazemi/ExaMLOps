import { useState, type ReactNode } from 'react'
import { Gauge } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { isAdmin } from '@/lib/auth'
import {
  useAutoscale,
  useSetAutoscale,
  useRouting,
  useSetRouting,
} from '@/lib/scaling'

/**
 * Scaling & Routing console (E4/E5 · ADR 0031/0039, dashboard-enterprise-rebuild · Serve group).
 *
 * Surfaces `exa serve autoscale` + `exa serve routing` in the UI over pure `platform.db` state
 * (config/policy/recorded-stats — no live Ray). Admins set a model's autoscale policy through the
 * shared `examlops.autoscale.set_policy` path and its routing config through
 * `examlops.data.gateway.set_gateway_config` (both audited `source=dashboard`). Viewers are
 * read-only — the write controls render disabled with an explanation (F15 R3).
 */
export function Scaling() {
  const admin = isAdmin()
  const [model, setModel] = useState('JPCP')

  return (
    <div className="p-6 space-y-8 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Gauge className="size-6 text-muted-foreground" aria-hidden="true" />
          Scaling &amp; Routing
        </h1>
        <p className="text-sm text-muted-foreground">
          Per-model autoscaling (min/max replicas, scale-to-zero) and inference routing
          (round-robin / KV-cache-aware). Configuration and recorded stats only — no live actuation.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-muted-foreground">
          Model
          <input
            aria-label="Scaling model"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className="mt-1 block w-40 rounded-md border border-border bg-transparent px-2 py-1 text-xs font-mono"
          />
        </label>
        {!admin && (
          <p className="text-xs text-muted-foreground pb-1">Requires the admin role to edit.</p>
        )}
      </div>

      <AutoscaleSection model={model} admin={admin} />
      <RoutingSection model={model} admin={admin} />
    </div>
  )
}

function AutoscaleSection({ model, admin }: { model: string; admin: boolean }) {
  const { data, isLoading, error } = useAutoscale(model)
  const setAutoscale = useSetAutoscale()
  const [min, setMin] = useState('1')
  const [max, setMax] = useState('4')
  const [metric, setMetric] = useState('queue_depth')
  const [target, setTarget] = useState('10')
  const [stz, setStz] = useState('0')
  const [msg, setMsg] = useState<string | null>(null)

  const submit = async () => {
    setMsg(null)
    const mn = Number(min)
    const mx = Number(max)
    if (!model.trim()) {
      setMsg('Model is required.')
      return
    }
    if (!(mn >= 0 && mx >= 1 && mx >= mn)) {
      setMsg('Require 0 ≤ min ≤ max and max ≥ 1.')
      return
    }
    try {
      await setAutoscale.mutateAsync({
        model: model.trim(),
        minReplicas: mn,
        maxReplicas: mx,
        targetMetric: metric.trim() || 'queue_depth',
        targetValue: Number(target) || 0,
        scaleToZeroAfterS: Number(stz) || 0,
      })
      setMsg(`Autoscale policy set for ${model} (${mn}–${mx} replicas).`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to set autoscale policy')
    }
  }

  const cfg = data?.config
  const savings = data?.savings
  const events = data?.events ?? []

  return (
    <section className="space-y-3">
      <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Autoscale</h2>

      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-muted-foreground">
          Min replicas
          <input aria-label="Min replicas" type="number" min="0" value={min} onChange={(e) => setMin(e.target.value)}
            disabled={!admin}
            className="mt-1 block w-24 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50" />
        </label>
        <label className="text-xs text-muted-foreground">
          Max replicas
          <input aria-label="Max replicas" type="number" min="1" value={max} onChange={(e) => setMax(e.target.value)}
            disabled={!admin}
            className="mt-1 block w-24 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50" />
        </label>
        <label className="text-xs text-muted-foreground">
          Metric
          <input aria-label="Target metric" value={metric} onChange={(e) => setMetric(e.target.value)}
            disabled={!admin}
            className="mt-1 block w-32 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50" />
        </label>
        <label className="text-xs text-muted-foreground">
          Target
          <input aria-label="Target value" type="number" step="0.1" value={target} onChange={(e) => setTarget(e.target.value)}
            disabled={!admin}
            className="mt-1 block w-24 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50" />
        </label>
        <label className="text-xs text-muted-foreground">
          Scale-to-zero after (s)
          <input aria-label="Scale to zero after" type="number" min="0" value={stz} onChange={(e) => setStz(e.target.value)}
            disabled={!admin}
            className="mt-1 block w-28 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50" />
        </label>
        <button onClick={submit} disabled={!admin || setAutoscale.isPending}
          title={admin ? undefined : 'Requires the admin role.'}
          className="rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50">
          Set policy
        </button>
      </div>

      {msg && (
        <p className="text-xs rounded-lg px-3 py-2" style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-2)' }}>
          {msg}
        </p>
      )}

      {error && <EmptyState title="Couldn't load autoscale config" description="The scaling endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-20 w-full" />}
      {!isLoading && !error && !cfg && (
        <EmptyState title="No autoscale policy" description={admin ? 'Declare one above.' : 'An admin can declare an autoscale policy here.'} />
      )}

      {cfg && (
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 text-xs">
          <Stat label="Replicas" value={`${cfg.min_replicas}–${cfg.max_replicas}`} />
          <Stat label="Target" value={`${cfg.target_metric}=${cfg.target_value}`} />
          <Stat label="Scale-to-zero" value={cfg.scale_to_zero_after_s ? `after ${cfg.scale_to_zero_after_s}s` : 'disabled'} />
          <Stat label="GPU fraction" value={String(cfg.gpu_fraction)} />
          {savings && (
            <Stat
              label="Scale-to-zero savings"
              value={`${savings.saved_gpu_hours} GPU-h · $${savings.saved_cost}`}
            />
          )}
        </div>
      )}

      {events.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
                <th className="px-3 py-2 font-medium">Time</th>
                <th className="px-3 py-2 font-medium">From</th>
                <th className="px-3 py-2 font-medium">To</th>
                <th className="px-3 py-2 font-medium">Reason</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id} className="border-b border-border/50">
                  <td className="px-3 py-2 text-xs text-muted-foreground">{e.ts}</td>
                  <td className="px-3 py-2 text-xs">{e.from_replicas}</td>
                  <td className="px-3 py-2 text-xs">{e.to_replicas}</td>
                  <td className="px-3 py-2 text-xs">{e.reason ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function RoutingSection({ model, admin }: { model: string; admin: boolean }) {
  const { data, isLoading, error } = useRouting(model)
  const setRouting = useSetRouting()
  const [mode, setMode] = useState('round_robin')
  const [slo, setSlo] = useState('')
  const [disaggregate, setDisaggregate] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  const submit = async () => {
    setMsg(null)
    if (!model.trim()) {
      setMsg('Model is required.')
      return
    }
    try {
      await setRouting.mutateAsync({
        model: model.trim(),
        mode,
        sloLatencyMs: slo.trim() === '' ? null : Number(slo),
        disaggregate,
      })
      setMsg(`Routing set for ${model} (${mode}).`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to set routing config')
    }
  }

  const cfg = data?.config
  const stats = data?.stats

  return (
    <section className="space-y-3">
      <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Routing</h2>

      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-muted-foreground">
          Mode
          <select aria-label="Routing mode" value={mode} onChange={(e) => setMode(e.target.value)}
            disabled={!admin}
            className="mt-1 block w-40 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50">
            <option value="round_robin">round_robin</option>
            <option value="cache_aware">cache_aware</option>
          </select>
        </label>
        <label className="text-xs text-muted-foreground">
          SLO latency (ms)
          <input aria-label="SLO latency ms" type="number" min="0" value={slo} onChange={(e) => setSlo(e.target.value)}
            disabled={!admin} placeholder="none"
            className="mt-1 block w-28 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50" />
        </label>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <input aria-label="Disaggregate prefill/decode" type="checkbox" checked={disaggregate}
            disabled={!admin} onChange={(e) => setDisaggregate(e.target.checked)} />
          Disaggregate prefill/decode
        </label>
        <button onClick={submit} disabled={!admin || setRouting.isPending}
          title={admin ? undefined : 'Requires the admin role.'}
          className="rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50">
          Set routing
        </button>
      </div>

      {msg && (
        <p className="text-xs rounded-lg px-3 py-2" style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-2)' }}>
          {msg}
        </p>
      )}

      {error && <EmptyState title="Couldn't load routing config" description="The scaling endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-20 w-full" />}
      {!isLoading && !error && !cfg && (
        <EmptyState title="No routing config" description={admin ? 'Declare one above.' : 'An admin can declare a routing config here.'} />
      )}

      {cfg && (
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 text-xs">
          <Stat
            label="Mode"
            value={
              <StatusPill status={cfg.mode === 'cache_aware' ? 'ok' : 'neutral'} label={cfg.mode} />
            }
          />
          <Stat label="SLO latency" value={cfg.slo_latency_ms != null ? `${cfg.slo_latency_ms} ms` : '—'} />
          <Stat label="Disaggregate" value={cfg.disaggregate ? 'yes' : 'no'} />
          {stats && (
            <Stat label="Cache hit rate" value={`${(stats.hit_rate * 100).toFixed(1)}% · ${stats.total} events`} />
          )}
        </div>
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
