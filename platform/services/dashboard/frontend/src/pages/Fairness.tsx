import { useState } from 'react'
import { Scale } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { isAdmin } from '@/lib/auth'
import { useFairnessConfigs, useSetFairness } from '@/lib/fairness'

/**
 * Fairness console (C8, dashboard-rebuild M5) — per-model slicing attributes + disparity thresholds.
 * Admins declare a config via the shared `examlops.data.governance.set_fairness_config` path
 * (audited). A gated config can block promotion when a slice breaches the threshold.
 */
export function Fairness() {
  const admin = isAdmin()
  const { data: configs = [], isLoading, error } = useFairnessConfigs()
  const setFairness = useSetFairness()
  const [model, setModel] = useState('')
  const [sliceAttrs, setSliceAttrs] = useState('')
  const [threshold, setThreshold] = useState('0.1')
  const [gate, setGate] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  const save = async () => {
    setMsg(null)
    const attrs = sliceAttrs.split(',').map((a) => a.trim()).filter(Boolean)
    const t = Number(threshold)
    if (!model.trim() || attrs.length === 0) {
      setMsg('Model and at least one slice attribute are required.')
      return
    }
    if (!(t >= 0 && t <= 1)) {
      setMsg('Threshold must be in [0, 1].')
      return
    }
    try {
      await setFairness.mutateAsync({ model: model.trim(), sliceAttrs: attrs, threshold: t, gatePromotion: gate })
      setMsg(`Fairness config saved for ${model}.`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to save fairness config')
    }
  }

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Scale className="size-6 text-muted-foreground" aria-hidden="true" />
          Fairness
        </h1>
        <p className="text-sm text-muted-foreground">
          Per-model slicing attributes + max allowed disparity. A gated config can block promotion when
          a subgroup breaches the threshold.
        </p>
      </div>

      {msg && (
        <p className="text-xs rounded-lg px-3 py-2" style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-2)' }}>
          {msg}
        </p>
      )}

      {admin && (
        <section className="space-y-2">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Configure fairness</h2>
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-xs text-muted-foreground">
              Model
              <input aria-label="Fairness model" value={model} onChange={(e) => setModel(e.target.value)}
                className="mt-1 block w-32 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <label className="text-xs text-muted-foreground">
              Threshold (0–1)
              <input aria-label="Fairness threshold" type="number" step="0.01" value={threshold} onChange={(e) => setThreshold(e.target.value)}
                className="mt-1 block w-24 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <input aria-label="Fairness gate promotion" type="checkbox" checked={gate} onChange={(e) => setGate(e.target.checked)} />
              Gate promotion
            </label>
          </div>
          <label className="block text-xs text-muted-foreground">
            Slice attributes (comma-separated)
            <input aria-label="Fairness slice attributes" value={sliceAttrs} onChange={(e) => setSliceAttrs(e.target.value)}
              placeholder="region, cluster, node_type" className="mt-1 block w-full rounded-md border border-border bg-transparent px-2 py-1 text-xs font-mono" />
          </label>
          <button onClick={save} disabled={setFairness.isPending}
            className="rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50">
            Save config
          </button>
        </section>
      )}

      {error && <EmptyState title="Couldn't load fairness configs" description="The fairness endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-24 w-full" />}
      {!isLoading && configs.length === 0 && !error && (
        <EmptyState title="No fairness configs" description={admin ? 'Configure one above.' : 'An admin can configure fairness slicing here.'} />
      )}

      {configs.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
                <th className="px-3 py-2 font-medium">Model</th>
                <th className="px-3 py-2 font-medium">Slice attrs</th>
                <th className="px-3 py-2 font-medium">Threshold</th>
                <th className="px-3 py-2 font-medium">Gate</th>
                <th className="px-3 py-2 font-medium">Enabled</th>
              </tr>
            </thead>
            <tbody>
              {configs.map((c) => (
                <tr key={c.model} className="border-b border-border/50">
                  <td className="px-3 py-2 font-mono text-xs">{c.model}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">{c.slice_attrs.join(', ')}</td>
                  <td className="px-3 py-2 text-xs">{c.threshold}</td>
                  <td className="px-3 py-2">
                    {c.gate_promotion ? <StatusPill status="warn" label="Gates" /> : <span className="text-xs text-muted-foreground">—</span>}
                  </td>
                  <td className="px-3 py-2">
                    <StatusPill status={c.enabled ? 'ok' : 'warn'} label={c.enabled ? 'On' : 'Off'} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
