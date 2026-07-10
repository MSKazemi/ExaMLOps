import { useState } from 'react'
import { Cpu, Server, Layers, Clock, Network } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { KpiTile } from '@/components/viz'
import { isAdmin } from '@/lib/auth'
import {
  useFacilityOverview,
  useFacilityQueue,
  useFleet,
  useClusterDecision,
  clusterStateTone,
  waitLabel,
  partitionTone,
  type FacilityOverview,
} from '@/lib/facility'

// ── fleet registry + approval gate (Phase 35b) ───────────────────────────────

function Fleet() {
  const { data, isLoading } = useFleet()
  const decision = useClusterDecision()
  const admin = isAdmin()
  const clusters = data?.clusters ?? []

  if (isLoading) return <Skeleton className="h-16 w-full" />
  if (clusters.length === 0) {
    return (
      <EmptyState
        title="No clusters registered"
        description="Discover and register one with the CLI: exa hpc connect <host> --name <n>."
      />
    )
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
            <th className="px-3 py-2 font-medium">Cluster</th>
            <th className="px-3 py-2 font-medium">Scheduler</th>
            <th className="px-3 py-2 font-medium">Host</th>
            <th className="px-3 py-2 font-medium">GPUs</th>
            <th className="px-3 py-2 font-medium">State</th>
            {admin && <th className="px-3 py-2 font-medium">Actions</th>}
          </tr>
        </thead>
        <tbody>
          {clusters.map((c) => (
            <tr key={c.name} className="border-b border-border/50">
              <td className="px-3 py-2 font-medium">{c.name}</td>
              <td className="px-3 py-2 text-muted-foreground">{c.scheduler ?? '—'}</td>
              <td className="px-3 py-2 font-mono text-xs text-muted-foreground">{c.host ?? '—'}</td>
              <td className="px-3 py-2 text-muted-foreground">{c.capabilities?.total_gpus ?? '—'}</td>
              <td className="px-3 py-2">
                <StatusPill status={clusterStateTone(c.state)} label={c.state} />
              </td>
              {admin && (
                <td className="px-3 py-2">
                  {c.state !== 'ACTIVE' && (
                    <button
                      className="mr-2 rounded-md border border-border px-2 py-1 text-xs hover:bg-muted disabled:opacity-50"
                      disabled={decision.isPending}
                      onClick={() => decision.mutate({ name: c.name, decision: 'approve' })}
                    >
                      Approve
                    </button>
                  )}
                  {c.state !== 'REJECTED' && (
                    <button
                      className="rounded-md border border-border px-2 py-1 text-xs hover:bg-muted disabled:opacity-50"
                      disabled={decision.isPending}
                      onClick={() => {
                        const reason = window.prompt(`Reject cluster '${c.name}'? Optional reason:`) ?? undefined
                        decision.mutate({ name: c.name, decision: 'reject', reason })
                      }}
                    >
                      Reject
                    </button>
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── partitions ─────────────────────────────────────────────────────────────────

function Partitions({ ov }: { ov: FacilityOverview }) {
  if (ov.partitions.length === 0) {
    return <EmptyState title="No partitions" description="No scheduler activity recorded yet." />
  }
  return (
    <div className="space-y-2">
      {ov.partitions.map((p) => (
        <div
          key={p.name}
          className="flex items-center justify-between rounded-lg border border-border px-4 py-2 text-sm"
        >
          <span className="font-medium">{p.name}</span>
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span>{p.running} running</span>
            <span>{p.queued} queued</span>
            <span>{p.gpusAllocated} GPU</span>
            <StatusPill status={partitionTone(p)} label={partitionTone(p) === 'warn' ? 'Backlog' : 'OK'} />
          </div>
        </div>
      ))}
    </div>
  )
}

// ── page ───────────────────────────────────────────────────────────────────────

export function FacilityConsole() {
  const [cluster, setCluster] = useState<string | null>(null)
  const { data: ovData, isLoading, error } = useFacilityOverview(cluster)
  const { data: qData } = useFacilityQueue(cluster)
  const ov = ovData?.facility
  const jobs = qData?.queue.jobs ?? []
  const partial = [...(ovData?._partial ?? []), ...(qData?._partial ?? [])]

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
            <Server className="size-6 text-muted-foreground" aria-hidden="true" />
            Facility Console
          </h1>
          <p className="text-sm text-muted-foreground">
            Scheduler-neutral HPC overview — node/GPU allocation and the job queue, live from the platform.
          </p>
        </div>
        {ov && ov.clusters.length > 1 && (
          <label className="text-xs text-muted-foreground flex items-center gap-2">
            Cluster
            <select
              className="rounded-md border border-border bg-background px-2 py-1 text-sm"
              value={cluster ?? ''}
              onChange={(e) => setCluster(e.target.value || null)}
            >
              <option value="">All</option>
              {ov.clusters.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {partial.length > 0 && (
        <StatusPill status="warn" label={`Partial data — ${partial.join(', ')} unavailable`} />
      )}

      {isLoading && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3" aria-label="Loading facility">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-20 w-full" />
          ))}
        </div>
      )}

      {error && (
        <EmptyState
          title="Couldn't load facility data"
          description="The facility BFF endpoint is unreachable. Check the platform database."
        />
      )}

      {ov && (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <KpiTile label="Nodes allocated" value={ov.nodesAllocated} icon={Server} />
            <KpiTile label="GPUs allocated" value={ov.gpusAllocated} icon={Cpu} />
            <KpiTile label="Jobs running" value={ov.jobsRunning} icon={Layers} />
            {/* Threshold colouring (F4 R2): a deep queue is the pressure signal. */}
            <KpiTile
              label="Queue depth"
              value={ov.queueDepth}
              icon={Clock}
              threshold={{ warn: 5, crit: 20 }}
            />
          </div>

          <section className="space-y-3">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest flex items-center gap-2">
              <Network className="size-3.5" aria-hidden="true" />
              Fleet — registered clusters
            </h2>
            <Fleet />
          </section>

          <section className="space-y-3">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
              Partitions
            </h2>
            <Partitions ov={ov} />
          </section>

          <section className="space-y-3">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
              Queue
            </h2>
            {jobs.length === 0 ? (
              <EmptyState title="Queue is empty" description="No jobs are currently waiting." />
            ) : (
              <div className="overflow-x-auto rounded-lg border border-border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
                      <th className="px-3 py-2 font-medium">Job</th>
                      <th className="px-3 py-2 font-medium">Cluster</th>
                      <th className="px-3 py-2 font-medium">Model</th>
                      <th className="px-3 py-2 font-medium">GPUs</th>
                      <th className="px-3 py-2 font-medium">Wait</th>
                    </tr>
                  </thead>
                  <tbody>
                    {jobs.map((j) => (
                      <tr key={j.id} className="border-b border-border/50">
                        <td className="px-3 py-2 font-mono text-xs">{j.id}</td>
                        <td className="px-3 py-2 text-muted-foreground">{j.cluster}</td>
                        <td className="px-3 py-2">{j.model}</td>
                        <td className="px-3 py-2 text-muted-foreground">{j.gpus ?? '—'}</td>
                        <td className="px-3 py-2 text-muted-foreground">{waitLabel(j.waitSec)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  )
}
