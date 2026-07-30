import { useState } from 'react'
import { SlidersHorizontal, Cpu, Blocks, History } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { isAdmin } from '@/lib/auth'
import { usePlatformOverview, useSetComputeCost } from '@/lib/platformOps'

/**
 * Platform Ops console (platform-ops-workbench M4) — the observe+act half of platform management (the
 * Jupyter workbench is the other half). Unifies the compute-node cost rate card, authored providers,
 * and the platform-management change feed, and lets an admin make the same governed changes from the
 * browser. Every write reuses `examlops.platform_admin` via the /api/v1/platform-ops router
 * (attributed to the principal, audited source=dashboard) — so the UI can never drift from CLI/notebook.
 */
export function PlatformOps() {
  const admin = isAdmin()
  const { data, isLoading, error } = usePlatformOverview()
  const setCost = useSetComputeCost()
  const [gpu, setGpu] = useState('')
  const [cpu, setCpu] = useState('')
  const [msg, setMsg] = useState<string | null>(null)

  const applyCost = async () => {
    setMsg(null)
    try {
      await setCost.mutateAsync({
        gpu_per_hour: gpu ? Number(gpu) : undefined,
        cpu_per_hour: cpu ? Number(cpu) : undefined,
      })
      setGpu('')
      setCpu('')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to set compute cost')
    }
  }

  if (isLoading) return <Skeleton className="m-6 h-64" />
  if (error || !data)
    return (
      <div className="p-6">
        <EmptyState title="Platform Ops unavailable" description={String(error ?? 'no data')} />
      </div>
    )

  const card = data.cost_card

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <SlidersHorizontal className="size-6 text-muted-foreground" aria-hidden="true" />
          Platform Ops
        </h1>
        <p className="text-sm text-muted-foreground">
          Manage the platform itself — compute-node cost, deployed calculation code, and the change
          feed. Every change is governed (RBAC → policy → audit) and shared with the CLI + workbench.
        </p>
      </div>

      {msg && <div className="text-sm text-destructive">{msg}</div>}

      {/* Compute-node cost rate card */}
      <section className="rounded-lg border p-4 space-y-3">
        <h2 className="font-semibold flex items-center gap-2">
          <Cpu className="size-4 text-muted-foreground" aria-hidden="true" /> Compute-node cost
        </h2>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
          <Stat label="GPU $/hour" value={card.gpu_rate} />
          <Stat label="CPU $/hour" value={card.cpu_rate} />
          <Stat label="Provider" value={card.provider ?? '—'} />
          <Stat label="Cost / GPU-hour" value={card.cost_per_gpu_hour ?? '—'} />
        </div>
        {card.methodology && (
          <p className="text-xs text-muted-foreground">{card.methodology}</p>
        )}
        {admin && (
          <div className="flex flex-wrap items-end gap-3 pt-2">
            <Field label="New GPU $/hour" value={gpu} onChange={setGpu} />
            <Field label="New CPU $/hour" value={cpu} onChange={setCpu} />
            <button
              type="button"
              onClick={applyCost}
              disabled={setCost.isPending || (!gpu && !cpu)}
              className="rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-50"
            >
              {setCost.isPending ? 'Saving…' : 'Set cost'}
            </button>
          </div>
        )}
      </section>

      {/* Authored providers */}
      <section className="rounded-lg border p-4 space-y-3">
        <h2 className="font-semibold flex items-center gap-2">
          <Blocks className="size-4 text-muted-foreground" aria-hidden="true" /> Deployed providers
        </h2>
        {data.providers.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No providers deployed yet. Author one in the Platform Ops workbench or via the CLI.
          </p>
        ) : (
          <ul className="divide-y text-sm">
            {data.providers.map((p) => (
              <li key={`${p.domain}/${p.name}`} className="flex items-center gap-3 py-2">
                <span className="font-mono">{p.domain}</span>
                <span className="font-medium">{p.name}</span>
                {p.active && <StatusPill status="ok" label="active" />}
                {!p.ok && <StatusPill status="error" label={`gate: ${p.error}`} />}
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Change feed */}
      <section className="rounded-lg border p-4 space-y-3">
        <h2 className="font-semibold flex items-center gap-2">
          <History className="size-4 text-muted-foreground" aria-hidden="true" /> Recent changes
        </h2>
        {data.changes.length === 0 ? (
          <p className="text-sm text-muted-foreground">No platform-management changes recorded yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-muted-foreground">
                  <th className="py-1 pr-4 font-medium">When</th>
                  <th className="py-1 pr-4 font-medium">Who</th>
                  <th className="py-1 pr-4 font-medium">Source</th>
                  <th className="py-1 pr-4 font-medium">Action</th>
                  <th className="py-1 font-medium">Target</th>
                </tr>
              </thead>
              <tbody>
                {data.changes.map((c, i) => (
                  <tr key={i} className="border-t">
                    <td className="py-1 pr-4 whitespace-nowrap">{String(c.ts).slice(0, 19)}</td>
                    <td className="py-1 pr-4">{c.actor ?? '—'}</td>
                    <td className="py-1 pr-4">{c.source}</td>
                    <td className="py-1 pr-4 font-mono">{c.action.replace('platform_admin:', '')}</td>
                    <td className="py-1">{c.target ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="font-semibold tabular-nums">{value}</div>
    </div>
  )
}

function Field({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (v: string) => void
}) {
  return (
    <label className="text-sm space-y-1">
      <span className="block text-xs text-muted-foreground">{label}</span>
      <input
        type="number"
        step="0.01"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-32 rounded-md border px-2 py-1"
      />
    </label>
  )
}
