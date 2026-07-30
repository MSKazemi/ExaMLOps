import { useState } from 'react'
import { Layers } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { isAdmin } from '@/lib/auth'
import { useFeatureViews, useApplyFeatureView } from '@/lib/features'

/**
 * Features console (A3, dashboard-rebuild M5) — the feature store's views (one train/serve
 * definition each). Admins register/patch a view via the shared `examlops.feature_store.apply_view`
 * path (audited). Was CLI-only (`exa feature apply`); read surface existed, write did not.
 */
export function Features() {
  const admin = isAdmin()
  const { data: views = [], isLoading, error } = useFeatureViews()
  const apply = useApplyFeatureView()
  const [name, setName] = useState('')
  const [entity, setEntity] = useState('')
  const [features, setFeatures] = useState('')
  const [ttl, setTtl] = useState('0')
  const [msg, setMsg] = useState<string | null>(null)

  const save = async () => {
    setMsg(null)
    const feats = features.split(',').map((f) => f.trim()).filter(Boolean)
    if (!name.trim() || !entity.trim() || feats.length === 0) {
      setMsg('Name, entity and at least one feature are required.')
      return
    }
    try {
      await apply.mutateAsync({ name: name.trim(), entity: entity.trim(), features: feats, ttlSeconds: Number(ttl) || 0 })
      setMsg(`Applied feature view ${name}.`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to apply feature view')
    }
  }

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Layers className="size-6 text-muted-foreground" aria-hidden="true" />
          Feature Store
        </h1>
        <p className="text-sm text-muted-foreground">
          Feature views — one definition shared by training and serving. Register or patch a view
          (name, entity, features, optional TTL).
        </p>
      </div>

      {msg && (
        <p className="text-xs rounded-lg px-3 py-2" style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-2)' }}>
          {msg}
        </p>
      )}

      {admin && (
        <section className="space-y-2">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Register / patch a view</h2>
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-xs text-muted-foreground">
              Name
              <input aria-label="Feature view name" value={name} onChange={(e) => setName(e.target.value)}
                className="mt-1 block w-40 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <label className="text-xs text-muted-foreground">
              Entity
              <input aria-label="Feature view entity" value={entity} onChange={(e) => setEntity(e.target.value)}
                placeholder="job" className="mt-1 block w-32 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <label className="text-xs text-muted-foreground">
              TTL (s)
              <input aria-label="Feature view ttl" type="number" value={ttl} onChange={(e) => setTtl(e.target.value)}
                className="mt-1 block w-24 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
          </div>
          <label className="block text-xs text-muted-foreground">
            Features (comma-separated)
            <input aria-label="Feature view features" value={features} onChange={(e) => setFeatures(e.target.value)}
              placeholder="cpu_util, mem_gb, gpu_hours" className="mt-1 block w-full rounded-md border border-border bg-transparent px-2 py-1 text-xs font-mono" />
          </label>
          <button onClick={save} disabled={apply.isPending}
            className="rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50">
            Apply view
          </button>
        </section>
      )}

      {error && <EmptyState title="Couldn't load feature views" description="The feature-store endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-24 w-full" />}
      {!isLoading && views.length === 0 && !error && (
        <EmptyState title="No feature views" description={admin ? 'Register one above.' : 'An admin can register feature views here.'} />
      )}

      {views.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
                <th className="px-3 py-2 font-medium">View</th>
                <th className="px-3 py-2 font-medium">Entity</th>
                <th className="px-3 py-2 font-medium">Features</th>
                <th className="px-3 py-2 font-medium">TTL</th>
                <th className="px-3 py-2 font-medium">Updated</th>
              </tr>
            </thead>
            <tbody>
              {views.map((v) => (
                <tr key={v.name} className="border-b border-border/50">
                  <td className="px-3 py-2 font-mono text-xs font-semibold">{v.name}</td>
                  <td className="px-3 py-2 text-xs">{v.entity}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">{v.features.join(', ')}</td>
                  <td className="px-3 py-2 text-xs">{v.ttl_seconds ? `${v.ttl_seconds}s` : '—'}</td>
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground">{v.updated_at?.slice(0, 19) ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
