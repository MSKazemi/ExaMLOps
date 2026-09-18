import { Sparkles, Users, Cpu, Network } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { KpiTile } from '@/components/viz'
import {
  useNextGenSummary,
  useFederatedRuns,
  useDevicePools,
  usePlacementDecisions,
  useBurstEvents,
  placementTone,
  privacyLabel,
  burstTone,
  poolCostLabel,
} from '@/lib/nextgen'

/** Section wrapper with a heading + icon, matching the other consoles. */
function Section({
  title,
  icon: Icon,
  children,
}: {
  title: string
  icon: React.ComponentType<{ className?: string }>
  children: React.ReactNode
}) {
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold tracking-tight flex items-center gap-2">
        <Icon className="size-5 text-muted-foreground" aria-hidden="true" />
        {title}
      </h2>
      {children}
    </section>
  )
}

function FederatedSection() {
  const { data, isLoading } = useFederatedRuns()
  if (isLoading) return <Skeleton className="h-24 w-full" />
  const runs = data ?? []
  if (runs.length === 0)
    return (
      <EmptyState
        title="No federated runs"
        description="Start one with exa federated init --site A --site B --dp --secure-agg."
      />
    )
  return (
    <div className="overflow-x-auto rounded-xl border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-muted-foreground">
            <th className="px-4 py-2">Run</th>
            <th className="px-4 py-2">Strategy</th>
            <th className="px-4 py-2">Privacy</th>
            <th className="px-4 py-2">Rounds</th>
            <th className="px-4 py-2">Status</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.run_id} className="border-t border-border">
              <td className="px-4 py-2 font-medium">{r.run_id}</td>
              <td className="px-4 py-2">{r.strategy}</td>
              <td className="px-4 py-2 text-muted-foreground">{privacyLabel(r)}</td>
              <td className="px-4 py-2">{r.rounds_completed}</td>
              <td className="px-4 py-2">
                <StatusPill status={r.status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function PoolsSection() {
  const { data, isLoading } = useDevicePools()
  if (isLoading) return <Skeleton className="h-24 w-full" />
  const pools = data ?? []
  if (pools.length === 0)
    return (
      <EmptyState
        title="No device pools"
        description="Register one with exa hardware add-pool <name> --accelerator amd --target hpc."
      />
    )
  return (
    <div className="overflow-x-auto rounded-xl border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-muted-foreground">
            <th className="px-4 py-2">Pool</th>
            <th className="px-4 py-2">Target</th>
            <th className="px-4 py-2">Accelerator</th>
            <th className="px-4 py-2">Count</th>
            <th className="px-4 py-2">Region</th>
            <th className="px-4 py-2">Cost / carbon</th>
          </tr>
        </thead>
        <tbody>
          {pools.map((p) => (
            <tr key={p.name} className="border-t border-border">
              <td className="px-4 py-2 font-medium">{p.name}</td>
              <td className="px-4 py-2">{p.target}</td>
              <td className="px-4 py-2">{p.accelerator}</td>
              <td className="px-4 py-2">{p.count}</td>
              <td className="px-4 py-2">{p.region ?? '—'}</td>
              <td className="px-4 py-2 text-muted-foreground">{poolCostLabel(p)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function PlacementsSection() {
  const { data, isLoading } = usePlacementDecisions()
  if (isLoading) return <Skeleton className="h-24 w-full" />
  const rows = data ?? []
  if (rows.length === 0)
    return <EmptyState title="No placements yet" description="Run exa hardware place <workload>." />
  return (
    <div className="overflow-x-auto rounded-xl border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-muted-foreground">
            <th className="px-4 py-2">Workload</th>
            <th className="px-4 py-2">Requested</th>
            <th className="px-4 py-2">Chosen</th>
            <th className="px-4 py-2">Decision</th>
            <th className="px-4 py-2">Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.workload}-${i}`} className="border-t border-border">
              <td className="px-4 py-2 font-medium">{r.workload}</td>
              <td className="px-4 py-2">{r.accelerator_requested ?? '—'}</td>
              <td className="px-4 py-2">{r.device_chosen ?? '—'}</td>
              <td className="px-4 py-2">
                <StatusPill status={placementTone(r.decision)} label={r.decision} />
              </td>
              <td className="px-4 py-2 text-muted-foreground">{r.reason ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function BurstsSection() {
  const { data, isLoading } = useBurstEvents()
  if (isLoading) return <Skeleton className="h-24 w-full" />
  const rows = data ?? []
  if (rows.length === 0)
    return (
      <EmptyState
        title="No cloud-burst attempts"
        description="Governed bursts appear here — allowed or blocked by data-residency."
      />
    )
  return (
    <div className="overflow-x-auto rounded-xl border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-muted-foreground">
            <th className="px-4 py-2">Workload</th>
            <th className="px-4 py-2">Residency</th>
            <th className="px-4 py-2">Outcome</th>
            <th className="px-4 py-2">Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.workload}-${i}`} className="border-t border-border">
              <td className="px-4 py-2 font-medium">{r.workload}</td>
              <td className="px-4 py-2">{r.residency ?? '—'}</td>
              <td className="px-4 py-2">
                <StatusPill status={burstTone(r.allowed)} label={r.allowed ? 'allowed' : 'blocked'} />
              </td>
              <td className="px-4 py-2 text-muted-foreground">{r.reason ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * NextGen console — surfaces the newest Next-Gen 40 capabilities (federated training E7,
 * heterogeneous-hardware placement E8, and a cross-feature roll-up) from /api/nextgen/*.
 * Read-only and degrades gracefully: each panel shows an EmptyState when its feature has
 * not been exercised.
 */
export function NextGen() {
  const { data: summary, isLoading, error } = useNextGenSummary()
  // A zero here is a claim — "no device pools are configured". When the summary could not be read
  // we have not earned that claim, so every tile shows a dash instead and the page says why. Same
  // rule the NOC wall follows: a dash is the absence of a number, a zero is an assertion about one.
  const kpi = (n: number | undefined) => (error ? '—' : (n ?? 0))

  return (
    <div className="p-6 space-y-8 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Sparkles className="size-6 text-muted-foreground" aria-hidden="true" />
          Next-Gen 40
        </h1>
        <p className="text-sm text-muted-foreground">
          Federated &amp; privacy-preserving training, heterogeneous-hardware placement, and the rest
          of the Next-Gen 40 capability set — read-only view of the platform&apos;s live state.
        </p>
      </div>

      {error && (
        <EmptyState
          title="Couldn't load the Next-Gen summary"
          description={`The platform state endpoint is unreachable (${String(error)}). The figures below are unknown, not zero.`}
        />
      )}

      {isLoading ? (
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3" aria-label="Loading summary">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-20 w-full" />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          <KpiTile label="Federated runs" value={kpi(summary?.federated_runs)} icon={Users} />
          <KpiTile label="Device pools" value={kpi(summary?.device_pools)} icon={Cpu} />
          <KpiTile label="Placements" value={kpi(summary?.placements)} icon={Network} />
          <KpiTile label="Autoscale configs" value={kpi(summary?.autoscale_configs)} />
          <KpiTile label="Distributed runs" value={kpi(summary?.distributed_runs)} />
          <KpiTile label="Feature views" value={kpi(summary?.feature_views)} />
        </div>
      )}

      <Section title="Federated training (E7)" icon={Users}>
        <FederatedSection />
      </Section>

      <Section title="Device pools (E8)" icon={Cpu}>
        <PoolsSection />
      </Section>

      <Section title="Placement decisions (E8)" icon={Network}>
        <PlacementsSection />
      </Section>

      <Section title="Governed cloud bursts (E8)" icon={Network}>
        <BurstsSection />
      </Section>
    </div>
  )
}
