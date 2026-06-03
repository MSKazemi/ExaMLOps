import { useState } from 'react'
import { Play, RefreshCw, Clock, CheckCircle2, XCircle, Loader2, AlertCircle } from 'lucide-react'
import { useMe, usePipelineDeployments, usePipelineRuns, useTriggerRun,
         type PrefectDeployment, type PrefectRun } from '@/lib/api'

function stateColor(stateType: string): string {
  switch (stateType.toUpperCase()) {
    case 'COMPLETED': return 'oklch(0.72 0.18 155)'
    case 'FAILED':
    case 'CRASHED': return 'oklch(0.66 0.22 25)'
    case 'RUNNING': return 'oklch(0.75 0.18 220)'
    case 'SCHEDULED':
    case 'PENDING': return 'oklch(0.75 0.18 80)'
    default: return 'var(--text-2)'
  }
}

function StateIcon({ stateType }: { stateType: string }) {
  switch (stateType.toUpperCase()) {
    case 'COMPLETED': return <CheckCircle2 size={14} />
    case 'FAILED':
    case 'CRASHED': return <XCircle size={14} />
    case 'RUNNING': return <Loader2 size={14} className="animate-spin" />
    default: return <Clock size={14} />
  }
}

function formatTime(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })
}

function modelFromDeployment(dep: PrefectDeployment): string {
  const m = dep.name.match(/^examlops-(.+)-nightly$/)
  return m ? m[1].toUpperCase() : dep.name
}

export function Pipelines() {
  const { data: me } = useMe()
  const isAdmin = me?.role === 'admin'

  const { data: deployments = [], isLoading: depsLoading, refetch: refetchDeps } =
    usePipelineDeployments()
  const { data: runs = [], isLoading: runsLoading, refetch: refetchRuns } =
    usePipelineRuns(30)
  const { mutate: triggerRun, isPending: triggering } = useTriggerRun()

  const [triggeredModel, setTriggeredModel] = useState<string | null>(null)

  function handleTrigger(dep: PrefectDeployment) {
    const modelName = modelFromDeployment(dep)
    setTriggeredModel(modelName)
    triggerRun(
      { model_name: modelName, dummy: true },
      { onSettled: () => setTriggeredModel(null) },
    )
  }

  const runsByDeployment = runs.reduce<Record<string, PrefectRun[]>>((acc, r) => {
    const key = r.deployment_id ?? 'unknown'
    ;(acc[key] ??= []).push(r)
    return acc
  }, {})

  return (
    <div className="p-6 space-y-8 max-w-5xl mx-auto">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold" style={{ color: 'var(--text-1)' }}>Pipelines</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-2)' }}>
            Prefect deployments — trigger or monitor training runs
          </p>
        </div>
        <button
          onClick={() => { refetchDeps(); refetchRuns() }}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm"
          style={{ background: 'var(--surface-1)', color: 'var(--text-2)' }}
        >
          <RefreshCw size={14} />
          Refresh
        </button>
      </div>

      <section>
        <h2 className="text-sm font-medium mb-3 uppercase tracking-wide"
            style={{ color: 'var(--text-2)' }}>
          Registered Deployments
        </h2>
        {depsLoading ? (
          <p className="text-sm" style={{ color: 'var(--text-2)' }}>Loading…</p>
        ) : deployments.length === 0 ? (
          <div className="rounded-xl p-6 text-center" style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}>
            <AlertCircle size={20} className="mx-auto mb-2" style={{ color: 'var(--text-2)' }} />
            <p className="text-sm" style={{ color: 'var(--text-2)' }}>
              No deployments found. Run <code className="text-xs px-1 py-0.5 rounded" style={{ background: 'var(--surface-1)' }}>exa pipeline deploy</code> first.
            </p>
          </div>
        ) : (
          <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
            <table className="w-full text-sm">
              <thead>
                <tr style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border)' }}>
                  {['Model', 'Deployment', 'Status', 'Schedule', 'Last Run', isAdmin ? 'Actions' : ''].map(h => (
                    <th key={h} className="text-left px-4 py-2.5 font-medium"
                        style={{ color: 'var(--text-2)' }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {deployments.map(dep => {
                  const modelName = modelFromDeployment(dep)
                  const depRuns = runsByDeployment[dep.id] ?? []
                  const lastRun = depRuns[0] ?? null
                  const schedule = dep.schedules?.[0]
                  return (
                    <tr key={dep.id} style={{ borderBottom: '1px solid var(--border)' }}>
                      <td className="px-4 py-3 font-mono font-semibold"
                          style={{ color: 'var(--text-1)' }}>{modelName}</td>
                      <td className="px-4 py-3" style={{ color: 'var(--text-2)' }}>{dep.name}</td>
                      <td className="px-4 py-3">
                        <span className="flex items-center gap-1.5"
                              style={{ color: dep.paused ? 'var(--text-2)' : 'oklch(0.72 0.18 155)' }}>
                          <span className="w-2 h-2 rounded-full"
                                style={{ background: dep.paused ? 'var(--text-2)' : 'oklch(0.72 0.18 155)' }} />
                          {dep.paused ? 'Paused' : dep.status ?? 'READY'}
                        </span>
                      </td>
                      <td className="px-4 py-3 font-mono text-xs"
                          style={{ color: 'var(--text-2)' }}>
                        {schedule?.active ? schedule.cron : '—'}
                      </td>
                      <td className="px-4 py-3">
                        {lastRun ? (
                          <span className="flex items-center gap-1.5"
                                style={{ color: stateColor(lastRun.state_type) }}>
                            <StateIcon stateType={lastRun.state_type} />
                            <span className="text-xs">{formatTime(lastRun.start_time)}</span>
                          </span>
                        ) : (
                          <span style={{ color: 'var(--text-2)' }}>—</span>
                        )}
                      </td>
                      {isAdmin && (
                        <td className="px-4 py-3">
                          <button
                            disabled={triggering && triggeredModel === modelName}
                            onClick={() => handleTrigger(dep)}
                            className="flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-medium transition-opacity disabled:opacity-50"
                            style={{ background: 'oklch(0.72 0.18 155 / 15%)',
                                     color: 'oklch(0.72 0.18 155)',
                                     border: '1px solid oklch(0.72 0.18 155 / 30%)' }}
                          >
                            {triggering && triggeredModel === modelName
                              ? <Loader2 size={12} className="animate-spin" />
                              : <Play size={12} />}
                            Trigger
                          </button>
                        </td>
                      )}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section>
        <h2 className="text-sm font-medium mb-3 uppercase tracking-wide"
            style={{ color: 'var(--text-2)' }}>
          Recent Runs (last 30)
        </h2>
        {runsLoading ? (
          <p className="text-sm" style={{ color: 'var(--text-2)' }}>Loading…</p>
        ) : runs.length === 0 ? (
          <p className="text-sm" style={{ color: 'var(--text-2)' }}>No runs yet.</p>
        ) : (
          <div className="space-y-1.5">
            {runs.map(run => (
              <div key={run.id}
                   className="flex items-center gap-4 px-4 py-2.5 rounded-lg"
                   style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}>
                <span style={{ color: stateColor(run.state_type) }}>
                  <StateIcon stateType={run.state_type} />
                </span>
                <span className="font-mono text-xs flex-1" style={{ color: 'var(--text-1)' }}>
                  {run.name}
                </span>
                <span className="text-xs" style={{ color: 'var(--text-2)' }}>
                  {run.state_name}
                </span>
                <span className="text-xs" style={{ color: 'var(--text-2)' }}>
                  {formatTime(run.start_time)}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
