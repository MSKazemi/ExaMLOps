import { useState } from 'react'
import { Network } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { KpiTile } from '@/components/viz/KpiTile'
import { AssetDag } from '@/components/viz/AssetDag'
import { assetState, useAssetGraph, type AssetNode } from '@/lib/assets'

const PILL: Record<string, { status: string; label: string }> = {
  fresh: { status: 'healthy', label: 'Fresh' },
  stale: { status: 'warn', label: 'Stale' },
  never: { status: 'critical', label: 'Never built' },
  undeclared: { status: 'unknown', label: 'Undeclared' },
}

/**
 * Software-defined assets (ADR 0036 clause 5).
 *
 * The asset DAG — datasets, features and models, each with the upstreams it is built from — and
 * which assets are stale and why. Read-only: rebuilding stays with `exa assets materialize`, which
 * runs through the scheduler and policy (see the router docstring for why).
 */
export function Assets() {
  const { data, isLoading, error } = useAssetGraph()
  const [selected, setSelected] = useState<string | null>(null)
  const assets = data?.assets ?? []
  const current = assets.find((a) => a.name === selected) ?? null

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Network className="size-6 text-muted-foreground" aria-hidden="true" />
          Assets
        </h1>
        <p className="text-sm text-muted-foreground">
          Datasets, features and models as one dependency graph, and which of them are stale and
          why. Rebuild with <code>exa assets materialize &lt;asset&gt;</code>.
        </p>
      </div>

      {isLoading && <Skeleton className="h-48 w-full" />}
      {error && (
        <EmptyState title="Couldn't load the asset graph" description="The assets endpoint is unreachable." />
      )}
      {data && assets.length === 0 && (
        <EmptyState
          title="No assets declared"
          description="Declare one with exa assets declare, or apply a feature view — it registers itself."
        />
      )}

      {data && assets.length > 0 && (
        <>
          <div className="grid grid-cols-3 gap-3">
            <KpiTile label="Assets" value={data.counts.total} />
            <KpiTile label="Fresh" value={data.counts.fresh} />
            <KpiTile label="Stale or never built" value={data.counts.stale} threshold={{ warn: 1, crit: 1 }} />
          </div>
          <AssetDag assets={assets} selected={selected} onSelect={setSelected} />
          {current ? (
            <AssetDetail asset={current} />
          ) : (
            <p className="text-sm text-muted-foreground">Select an asset to see why it is fresh or stale.</p>
          )}
        </>
      )}
    </div>
  )
}

function AssetDetail({ asset }: { asset: AssetNode }) {
  const pill = PILL[assetState(asset)]
  return (
    <section className="rounded-lg border p-4 space-y-2" aria-label={`Asset ${asset.name}`}>
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-semibold">{asset.name}</h2>
        <span className="text-xs text-muted-foreground">{asset.kind} · v{asset.version}</span>
        <StatusPill status={pill.status} label={pill.label} />
      </div>
      {asset.description && <p className="text-sm text-muted-foreground">{asset.description}</p>}
      {asset.reasons.length > 0 && (
        <ul className="list-disc ml-5 text-sm">
          {asset.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-muted-foreground">Built from</dt>
        <dd>{asset.deps.join(', ') || '— (a source)'}</dd>
        <dt className="text-muted-foreground">Feeds</dt>
        <dd>{asset.dependents.join(', ') || '—'}</dd>
        <dt className="text-muted-foreground">Last materialized</dt>
        <dd>{asset.lastMaterializedAt ?? 'never'}</dd>
      </dl>
      {asset.undeclaredDeps.length > 0 && (
        <StatusPill status="warn" label={`Undeclared upstream: ${asset.undeclaredDeps.join(', ')}`} />
      )}
    </section>
  )
}
