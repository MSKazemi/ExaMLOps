import { useState } from 'react'
import { Target } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { isAdmin } from '@/lib/auth'
import { useSlos, useSetSlo } from '@/lib/slo'

/**
 * SLOs console (C6 / ADR 0023, dashboard-rebuild M3) — model-quality SLO specs + live status. Admins
 * define/update a target via the shared `examlops.slo.apply_spec` path (audited). Live SLI / error
 * budget is best-effort (from `slo_samples`); shown as "no data" when none exist.
 */
export function Slo() {
  const admin = isAdmin()
  const { data: specs = [], isLoading, error } = useSlos()
  const setSlo = useSetSlo()
  const [model, setModel] = useState('')
  const [name, setName] = useState('availability')
  const [target, setTarget] = useState('0.99')
  const [gate, setGate] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  const create = async () => {
    setMsg(null)
    const t = Number(target)
    if (!model.trim() || !name.trim()) {
      setMsg('Model and name are required.')
      return
    }
    if (!(t > 0 && t <= 1)) {
      setMsg('Target must be in (0, 1].')
      return
    }
    try {
      await setSlo.mutateAsync({ model: model.trim(), name: name.trim(), target: t, gatePromotion: gate })
      setMsg(`SLO ${model}/${name} set (target ${t}).`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to set SLO')
    }
  }

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Target className="size-6 text-muted-foreground" aria-hidden="true" />
          Model-Quality SLOs
        </h1>
        <p className="text-sm text-muted-foreground">
          Declarative SLO targets + live error-budget status. A gated SLO can block promotion when its
          budget is exhausted.
        </p>
      </div>

      {msg && (
        <p className="text-xs rounded-lg px-3 py-2" style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-2)' }}>
          {msg}
        </p>
      )}

      {admin && (
        <section className="space-y-2">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Define an SLO</h2>
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-xs text-muted-foreground">
              Model
              <input aria-label="SLO model" value={model} onChange={(e) => setModel(e.target.value)}
                className="mt-1 block w-32 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <label className="text-xs text-muted-foreground">
              Name
              <input aria-label="SLO name" value={name} onChange={(e) => setName(e.target.value)}
                className="mt-1 block w-36 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <label className="text-xs text-muted-foreground">
              Target (0–1)
              <input aria-label="SLO target" type="number" step="0.001" value={target} onChange={(e) => setTarget(e.target.value)}
                className="mt-1 block w-24 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <input aria-label="Gate promotion" type="checkbox" checked={gate} onChange={(e) => setGate(e.target.checked)} />
              Gate promotion
            </label>
            <button onClick={create} disabled={setSlo.isPending}
              className="rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50">
              Set SLO
            </button>
          </div>
        </section>
      )}

      {error && <EmptyState title="Couldn't load SLOs" description="The SLO endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-24 w-full" />}
      {!isLoading && specs.length === 0 && !error && (
        <EmptyState title="No SLOs defined" description={admin ? 'Define one above.' : 'An admin can define model-quality SLOs here.'} />
      )}

      {specs.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
                <th className="px-3 py-2 font-medium">Model</th>
                <th className="px-3 py-2 font-medium">SLO</th>
                <th className="px-3 py-2 font-medium">Target</th>
                <th className="px-3 py-2 font-medium">Window</th>
                <th className="px-3 py-2 font-medium">Gate</th>
                <th className="px-3 py-2 font-medium">Live status</th>
              </tr>
            </thead>
            <tbody>
              {specs.map((s) => (
                <tr key={`${s.model}/${s.name}`} className="border-b border-border/50">
                  <td className="px-3 py-2 font-mono text-xs">{s.model}</td>
                  <td className="px-3 py-2 text-xs">{s.name}</td>
                  <td className="px-3 py-2 text-xs">{(s.target * 100).toFixed(2)}%</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">{s.window}</td>
                  <td className="px-3 py-2">
                    {s.gate_promotion ? <StatusPill status="warn" label="Gates" /> : <span className="text-xs text-muted-foreground">—</span>}
                  </td>
                  <td className="px-3 py-2">
                    {s.status ? (
                      <span className="inline-flex items-center gap-2 text-xs">
                        {s.status.measured === false ? (
                          <StatusPill status="warn" label="Unmeasured" />
                        ) : (
                          <StatusPill status={s.status.ok ? 'ok' : 'critical'} label={s.status.ok ? 'Meeting' : 'Breaching'} />
                        )}
                        <span className="text-muted-foreground">
                          {s.status.measured === false
                            ? 'no samples recorded'
                            : `SLI ${(s.status.sli * 100).toFixed(2)}% · budget ${(s.status.budgetRemaining * 100).toFixed(0)}%`}
                        </span>
                      </span>
                    ) : (
                      <span className="text-xs text-muted-foreground">no data</span>
                    )}
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
