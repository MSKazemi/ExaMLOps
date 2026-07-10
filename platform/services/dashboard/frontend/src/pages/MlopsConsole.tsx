import { useState } from 'react'
import { Boxes, ShieldCheck } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import {
  useMlopsRegistry,
  useMlopsPromotion,
  promotionVerdict,
  freshnessLabel,
  type ModelRow,
} from '@/lib/mlops'

// ── promotion panel (guided gate, F9 R4) ──────────────────────────────────────

function PromotionPanel({ name }: { name: string }) {
  const { data, isLoading } = useMlopsPromotion(name)
  if (isLoading) return <Skeleton className="h-24 w-full" />
  if (!data) return null
  const chk = data.promotion
  const verdict = promotionVerdict(chk)
  return (
    <div className="rounded-lg border border-border p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <ShieldCheck className="size-4 text-muted-foreground" aria-hidden="true" />
          <h3 className="text-sm font-semibold">Promotion gate — {chk.model}</h3>
        </div>
        <StatusPill status={verdict.tone === 'ok' ? 'ok' : 'warn'} label={verdict.text} />
      </div>
      {chk.policy.metric && (
        <p className="text-xs text-muted-foreground">
          Policy: promote {chk.policy.fromAlias} → {chk.policy.toAlias} when{' '}
          <code className="font-mono">
            {chk.policy.metric} {chk.policy.operator} {chk.policy.threshold}
          </code>
        </p>
      )}
      {chk.policy.reasons.length > 0 && (
        <ul className="text-xs text-muted-foreground list-disc pl-5 space-y-0.5">
          {chk.policy.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
      <p className="text-xs text-muted-foreground">
        Approval: {chk.approval.required ? `required (${chk.approval.state ?? 'pending'})` : 'not required'}
      </p>
    </div>
  )
}

// ── registry grid (F9 R1) ─────────────────────────────────────────────────────

function RegistryGrid({
  rows,
  selected,
  onSelect,
}: {
  rows: ModelRow[]
  selected: string | null
  onSelect: (name: string) => void
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
            <th className="px-3 py-2 font-medium">Model</th>
            <th className="px-3 py-2 font-medium">Ver</th>
            <th className="px-3 py-2 font-medium">Stage</th>
            <th className="px-3 py-2 font-medium">Health</th>
            <th className="px-3 py-2 font-medium">Governed</th>
            <th className="px-3 py-2 font-medium">Freshness</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.name}
              onClick={() => onSelect(r.mlflowName)}
              aria-selected={selected === r.mlflowName}
              className={`cursor-pointer border-b border-border/50 transition-colors hover:bg-muted/40 ${
                selected === r.mlflowName ? 'bg-muted/60' : ''
              }`}
            >
              <td className="px-3 py-2 font-medium">{r.name}</td>
              <td className="px-3 py-2 text-muted-foreground">{r.version ?? '—'}</td>
              <td className="px-3 py-2 text-muted-foreground">{r.stage}</td>
              <td className="px-3 py-2">
                <StatusPill status={r.health} />
              </td>
              <td className="px-3 py-2">
                {r.governed ? (
                  <StatusPill status="ok" label="Yes" />
                ) : (
                  <StatusPill status="warn" label="No policy" />
                )}
              </td>
              <td className="px-3 py-2 text-xs text-muted-foreground font-mono">
                {freshnessLabel(r.freshness)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── page ───────────────────────────────────────────────────────────────────────

export function MlopsConsole() {
  const { data, isLoading, error } = useMlopsRegistry()
  const [selected, setSelected] = useState<string | null>(null)
  const rows = data?.registry.rows ?? []
  const partial = data?._partial ?? []

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Boxes className="size-6 text-muted-foreground" aria-hidden="true" />
          MLOps Console
        </h1>
        <p className="text-sm text-muted-foreground">
          Model registry, lifecycle health, and guided promotion gates — sourced live from the platform.
        </p>
      </div>

      {partial.length > 0 && (
        <StatusPill status="warn" label={`Partial data — ${partial.join(', ')} unavailable`} />
      )}

      {isLoading && (
        <div className="space-y-2" aria-label="Loading registry">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      )}

      {error && (
        <EmptyState
          title="Couldn't load the registry"
          description="The MLOps BFF endpoint is unreachable. Check the control plane and platform database."
        />
      )}

      {!isLoading && !error && rows.length === 0 && (
        <EmptyState
          title="No models tracked yet"
          description="Once a model records drift, cost, traffic, or a promotion policy, it appears here."
        />
      )}

      {rows.length > 0 && (
        <div className="grid gap-6 lg:grid-cols-[1fr_20rem]">
          <RegistryGrid rows={rows} selected={selected} onSelect={setSelected} />
          <div className="space-y-3">
            {selected ? (
              <PromotionPanel name={selected} />
            ) : (
              <EmptyState
                title="Select a model"
                description="Pick a model to see its guided promotion gate."
              />
            )}
          </div>
        </div>
      )}
    </div>
  )
}
