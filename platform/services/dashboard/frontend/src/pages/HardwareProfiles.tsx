import { useState } from 'react'
import { CircuitBoard, AlertTriangle } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import {
  useHardwareProfiles,
  useHardwareProfilesInUse,
  useHardwareProfilesHistory,
  profileShape,
  type ProfileStatus,
  type InUseEntry,
  type HistoryEntry,
} from '@/lib/hardwareProfiles'

/**
 * Hardware Profiles console (ADR 0157 Phase 4) — closes the ADR's own named gap: "the resolution
 * status is not yet visible wherever a profile is used ... only workbench rows show it" and "the
 * frontend ignores the report's truncation flag". Catalog, in-use (attention-first) and the
 * append-only resolution ledger, all reads over the router's existing endpoints — nothing here
 * adds a new backend call. Authoring (`POST`/`DELETE`) stays a CLI-only surface for now
 * (`exa hardware profile set|delete`) — a separate, smaller gap this page does not attempt.
 */

function statusTone(status: ProfileStatus): 'ok' | 'warn' | 'critical' | 'neutral' {
  if (status === 'verified') return 'ok'
  if (status === 'degraded') return 'warn'
  if (status === 'unresolvable' || status === 'missing') return 'critical'
  return 'neutral'
}

function CatalogSection() {
  const { data, isLoading, error } = useHardwareProfiles()
  if (isLoading) return <Skeleton className="h-24 w-full" />
  if (error) return <EmptyState title="Couldn't load the catalog" description="The hardware-profiles endpoint is unreachable." />
  if (!data || data.length === 0) {
    return (
      <EmptyState
        title="No hardware profiles"
        description="Define one with the CLI: exa hardware profile set <name> --cpu ... --memory-gb ..."
      />
    )
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
            <th className="px-3 py-2 font-medium">Profile</th>
            <th className="px-3 py-2 font-medium">Shape</th>
            <th className="px-3 py-2 font-medium">Applicability</th>
            <th className="px-3 py-2 font-medium">Accelerator</th>
            <th className="px-3 py-2 font-medium">Description</th>
          </tr>
        </thead>
        <tbody>
          {data.map((p) => (
            <tr key={p.name} className="border-b border-border/50">
              <td className="px-3 py-2 font-mono text-xs">{p.name} <span className="text-muted-foreground">v{p.version}</span></td>
              <td className="px-3 py-2 text-xs text-muted-foreground">{profileShape(p)}</td>
              <td className="px-3 py-2 text-xs">{p.applicability.join(', ')}</td>
              <td className="px-3 py-2 text-xs text-muted-foreground">
                {p.gpuCount > 0 ? `${p.acceleratorFamily}${p.acceleratorModelHint ? ` (${p.acceleratorModelHint})` : ''}` : '—'}
              </td>
              <td className="px-3 py-2 text-xs text-muted-foreground truncate max-w-xs">{p.description || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function InUseRow({ e }: { e: InUseEntry }) {
  return (
    <tr className="border-b border-border/50">
      <td className="px-3 py-2 text-xs">{e.consumer}</td>
      <td className="px-3 py-2 font-mono text-xs">{e.consumer_ref}</td>
      <td className="px-3 py-2 text-xs text-muted-foreground">{e.project ?? '—'}</td>
      <td className="px-3 py-2 font-mono text-xs">{e.name} v{e.version}</td>
      <td className="px-3 py-2">
        <StatusPill status={statusTone(e.status)} label={e.status} />
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground truncate max-w-xs" title={e.reason}>{e.reason || '—'}</td>
    </tr>
  )
}

function InUseSection({ project }: { project: string | null }) {
  const { data, isLoading, error } = useHardwareProfilesInUse(project ?? undefined)
  const [attentionOnly, setAttentionOnly] = useState(true)
  if (isLoading) return <Skeleton className="h-24 w-full" />
  if (error) return <EmptyState title="Couldn't load in-use profiles" description="The hardware-profiles endpoint is unreachable." />
  const rows = attentionOnly ? data?.attention ?? [] : data?.entries ?? []
  return (
    <div className="space-y-3">
      {data?.truncated && (
        <p className="flex items-center gap-1.5 text-xs rounded-lg px-3 py-2"
          style={{ background: 'oklch(0.80 0.16 85 / 12%)', border: '1px solid oklch(0.80 0.16 85 / 28%)', color: 'var(--warning-text)' }}>
          <AlertTriangle className="size-3.5 shrink-0" aria-hidden="true" />
          More consumers matched than this read returned — this is a partial view, not the complete set.
        </p>
      )}
      <div className="flex items-center gap-3 flex-wrap text-xs text-muted-foreground">
        {data && (
          <span>
            {Object.entries(data.counts).map(([s, n]) => `${s}: ${n}`).join(' · ') || 'no active consumers'}
          </span>
        )}
        <label className="flex items-center gap-1.5 ml-auto">
          <input type="checkbox" checked={attentionOnly} onChange={(e) => setAttentionOnly(e.target.checked)} />
          Needs attention only
        </label>
      </div>
      {rows.length === 0 ? (
        <EmptyState
          title={attentionOnly ? 'Nothing needs attention' : 'No profiles in use'}
          description={attentionOnly ? 'Every bound profile resolved cleanly.' : 'No running workbench or recent training/serving is bound to a profile.'}
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
                <th className="px-3 py-2 font-medium">Consumer</th>
                <th className="px-3 py-2 font-medium">Ref</th>
                <th className="px-3 py-2 font-medium">Project</th>
                <th className="px-3 py-2 font-medium">Profile</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Reason</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e, i) => <InUseRow key={`${e.consumer}-${e.consumer_ref}-${i}`} e={e} />)}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function HistoryRow({ h }: { h: HistoryEntry }) {
  return (
    <tr className="border-b border-border/50">
      <td className="px-3 py-2 text-xs text-muted-foreground whitespace-nowrap">{h.ts}</td>
      <td className="px-3 py-2 text-xs">{h.consumer}</td>
      <td className="px-3 py-2 font-mono text-xs">{h.consumer_ref}</td>
      <td className="px-3 py-2 font-mono text-xs">{h.name} v{h.version}</td>
      <td className="px-3 py-2">
        <StatusPill status={statusTone(h.status)} label={h.status} />
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground truncate max-w-xs" title={h.reason}>{h.reason || '—'}</td>
    </tr>
  )
}

function HistorySection() {
  const [name, setName] = useState('')
  const { data, isLoading, error } = useHardwareProfilesHistory({ name: name.trim() || undefined, limit: 100 })
  return (
    <div className="space-y-3">
      <label className="text-xs text-muted-foreground">
        Filter by profile name
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="gpu-small"
          aria-label="Filter history by profile name"
          className="mt-1 block w-48 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
      </label>
      {isLoading ? (
        <Skeleton className="h-24 w-full" />
      ) : error ? (
        <EmptyState title="Couldn't load the resolution ledger" description="The hardware-profiles endpoint is unreachable." />
      ) : !data || data.length === 0 ? (
        <EmptyState title="No resolutions recorded" description="Nothing has resolved a hardware profile yet." />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
                <th className="px-3 py-2 font-medium">When</th>
                <th className="px-3 py-2 font-medium">Consumer</th>
                <th className="px-3 py-2 font-medium">Ref</th>
                <th className="px-3 py-2 font-medium">Profile</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Reason</th>
              </tr>
            </thead>
            <tbody>
              {data.map((h) => <HistoryRow key={h.id} h={h} />)}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export function HardwareProfiles() {
  const [tab, setTab] = useState<'catalog' | 'in-use' | 'history'>('in-use')
  const attention = useHardwareProfilesInUse()
  const attentionCount = attention.data?.attention.length ?? 0

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <CircuitBoard className="size-6 text-muted-foreground" aria-hidden="true" />
          Hardware Profiles
        </h1>
        <p className="text-sm text-muted-foreground">
          Named resource+runtime bundles workbenches, training runs and serving deployments
          reference instead of restating raw CPU/memory/GPU flags — and whether each binding
          actually resolved.
        </p>
      </div>

      <div className="flex items-center gap-1 border-b border-border">
        {(['in-use', 'catalog', 'history'] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className="px-3 py-2 text-sm font-medium border-b-2 -mb-px transition-colors"
            style={t === tab
              ? { borderColor: 'var(--accent-text)', color: 'var(--accent-text)' }
              : { borderColor: 'transparent', color: 'var(--muted-foreground)' }}>
            {t === 'in-use' ? `In use${attentionCount > 0 ? ` (${attentionCount})` : ''}` : t === 'catalog' ? 'Catalog' : 'History'}
          </button>
        ))}
      </div>

      {tab === 'catalog' && <CatalogSection />}
      {tab === 'in-use' && <InUseSection project={null} />}
      {tab === 'history' && <HistorySection />}
    </div>
  )
}
