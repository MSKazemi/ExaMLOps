import { useState } from 'react'
import { Activity, RefreshCw, Target, Eraser, Power } from 'lucide-react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from '@/lib/api'
import { EmptyState } from '@/components/ui/empty-state'
import { Skeleton } from '@/components/ui/skeleton'
import { isAdmin } from '@/lib/auth'
import {
  useSetDriftBaseline,
  useResetDrift,
  useSetAutoRetrain,
  useSetInputBaseline,
  useResetInputDrift,
} from '@/lib/drift'

/** Shared loading placeholder for the drift tables (F3 Skeleton convention). */
function TableSkeleton() {
  return (
    <div
      className="space-y-2 rounded-xl p-4"
      style={{ border: '1px solid var(--border)' }}
      aria-label="Loading"
    >
      {Array.from({ length: 4 }).map((_, i) => (
        <Skeleton key={i} className="h-8 w-full" />
      ))}
    </div>
  )
}

interface DriftStatus {
  model: string
  live_mean: number
  live_std: number
  baseline_mean: number | null
  z_score: number
  status: string
  n_snapshots: number
}

interface InputDriftStatus {
  model: string
  live_norm_mean: number
  live_emb_mean: number
  live_emb_std: number
  max_z: number
  status: string
  n_snapshots: number
}

interface AutoRetrain {
  model: string
  enabled: number
  min_z_score: number
  dataset_name: string
  cooldown_s: number
  last_triggered: string | null
}

function statusStyle(status: string): React.CSSProperties {
  const s = status.toUpperCase()
  if (s === 'CRITICAL') return { color: 'var(--error-text, #ef4444)' }
  if (s === 'WARNING') return { color: 'var(--warning-text, #f59e0b)' }
  if (s === 'OK') return { color: 'var(--success-text, #22c55e)' }
  return { color: 'var(--muted-foreground)' }
}

function PredictionDriftTab({ onRefresh }: { onRefresh: () => void }) {
  const { data: rows = [], isLoading, error } = useQuery<DriftStatus[]>({
    queryKey: ['drift-status'],
    queryFn: () => apiFetch<DriftStatus[]>('/api/drift/status'),
  })
  const admin = isAdmin()
  const baseline = useSetDriftBaseline()
  const reset = useResetDrift()
  const [msg, setMsg] = useState<string | null>(null)

  const runBaseline = async (model: string) => {
    setMsg(null)
    try {
      const r = await baseline.mutateAsync(model)
      setMsg(`Baseline set for ${model} (μ=${r.baseline.mean?.toFixed(3)}, n=${r.baseline.n}).`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to set baseline')
    }
  }
  const runReset = async (model: string) => {
    if (!window.confirm(`Clear all drift snapshots for ${model}? The baseline is kept.`)) return
    setMsg(null)
    try {
      const r = await reset.mutateAsync(model)
      setMsg(`Cleared ${r.cleared} snapshot(s) for ${model}.`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to reset')
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">Prediction score drift vs. stored baseline.</p>
        <button
          onClick={onRefresh}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs"
          style={{ background: 'var(--surface-1)', color: 'var(--text-2)', border: '1px solid var(--border)' }}
        >
          <RefreshCw size={12} /> Refresh
        </button>
      </div>

      {error && (
        <p className="text-sm rounded-lg px-4 py-3"
          style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
          Failed to load drift status.
        </p>
      )}

      {msg && (
        <p className="text-xs rounded-lg px-3 py-2"
          style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-2)' }}>
          {msg}
        </p>
      )}

      {isLoading && <TableSkeleton />}

      {!isLoading && !error && rows.length === 0 && (
        <EmptyState
          icon={Activity}
          title="No drift snapshots yet"
          description="Run `exa drift baseline <MODEL>` to set a baseline."
        />
      )}

      {rows.length > 0 && (
        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
          <table className="w-full text-sm">
            <thead style={{ background: 'var(--surface-1)' }}>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="px-4 py-2.5">Model</th>
                <th className="px-4 py-2.5">Live μ</th>
                <th className="px-4 py-2.5">Live σ</th>
                <th className="px-4 py-2.5">Baseline μ</th>
                <th className="px-4 py-2.5">Z-score</th>
                <th className="px-4 py-2.5">Status</th>
                <th className="px-4 py-2.5">Snapshots</th>
                {admin && <th className="px-4 py-2.5 text-right">Actions</th>}
              </tr>
            </thead>
            <tbody style={{ background: 'var(--surface-0)' }}>
              {rows.map(row => {
                const busy = (baseline.isPending && baseline.variables === row.model) ||
                  (reset.isPending && reset.variables === row.model)
                return (
                <tr key={row.model} className="border-t" style={{ borderColor: 'var(--border-sm)' }}>
                  <td className="px-4 py-3 font-mono font-semibold text-xs">{row.model}</td>
                  <td className="px-4 py-3 font-mono text-xs">{row.live_mean.toFixed(4)}</td>
                  <td className="px-4 py-3 font-mono text-xs">{row.live_std.toFixed(4)}</td>
                  <td className="px-4 py-3 font-mono text-xs text-muted-foreground">
                    {row.baseline_mean != null ? row.baseline_mean.toFixed(4) : '—'}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">{row.z_score.toFixed(2)}</td>
                  <td className="px-4 py-3 text-xs font-semibold" style={statusStyle(row.status)}>
                    {row.status}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-muted-foreground">{row.n_snapshots}</td>
                  {admin && (
                    <td className="px-4 py-3 text-right whitespace-nowrap">
                      <button onClick={() => runBaseline(row.model)} disabled={busy} title="Set current stats as baseline"
                        className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium mr-1.5 disabled:opacity-50"
                        style={{ background: 'oklch(0.64 0.20 265 / 12%)', border: '1px solid oklch(0.64 0.20 265 / 25%)', color: 'var(--accent-text)' }}>
                        <Target className="w-3 h-3" /> Baseline
                      </button>
                      <button onClick={() => runReset(row.model)} disabled={busy} title="Clear drift snapshots"
                        className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium disabled:opacity-50"
                        style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
                        <Eraser className="w-3 h-3" /> Reset
                      </button>
                    </td>
                  )}
                </tr>
              )})}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function InputDriftTab({ onRefresh }: { onRefresh: () => void }) {
  const { data: rows = [], isLoading, error } = useQuery<InputDriftStatus[]>({
    queryKey: ['drift-input-status'],
    queryFn: () => apiFetch<InputDriftStatus[]>('/api/drift/input-status'),
  })
  const admin = isAdmin()
  const baseline = useSetInputBaseline()
  const reset = useResetInputDrift()
  const [msg, setMsg] = useState<string | null>(null)

  const runBaseline = async (model: string) => {
    setMsg(null)
    try {
      const r = await baseline.mutateAsync(model)
      setMsg(`Input baseline set for ${model} (norm μ=${r.baseline.norm_mean?.toFixed(3)}, n=${r.baseline.n}).`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to set input baseline')
    }
  }
  const runReset = async (model: string) => {
    if (!window.confirm(`Clear all input snapshots for ${model}? The baseline is kept.`)) return
    setMsg(null)
    try {
      const r = await reset.mutateAsync(model)
      setMsg(`Cleared ${r.cleared} input snapshot(s) for ${model}.`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to reset input drift')
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">Embedding distribution drift vs. stored baseline.</p>
        <button
          onClick={onRefresh}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs"
          style={{ background: 'var(--surface-1)', color: 'var(--text-2)', border: '1px solid var(--border)' }}
        >
          <RefreshCw size={12} /> Refresh
        </button>
      </div>

      {error && (
        <p className="text-sm rounded-lg px-4 py-3"
          style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
          Failed to load input drift status.
        </p>
      )}

      {msg && (
        <p className="text-xs rounded-lg px-3 py-2"
          style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-2)' }}>
          {msg}
        </p>
      )}

      {isLoading && <TableSkeleton />}

      {!isLoading && !error && rows.length === 0 && (
        <EmptyState
          icon={Activity}
          title="No input drift snapshots yet"
          description={
            admin
              ? 'Once the bridge collects embeddings, use the Baseline action to set a baseline.'
              : 'Run `exa drift input baseline <MODEL>` to set a baseline.'
          }
        />
      )}

      {rows.length > 0 && (
        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
          <table className="w-full text-sm">
            <thead style={{ background: 'var(--surface-1)' }}>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="px-4 py-2.5">Model</th>
                <th className="px-4 py-2.5">Norm μ</th>
                <th className="px-4 py-2.5">Emb μ</th>
                <th className="px-4 py-2.5">Emb σ</th>
                <th className="px-4 py-2.5">Max Z</th>
                <th className="px-4 py-2.5">Status</th>
                <th className="px-4 py-2.5">Snapshots</th>
                {admin && <th className="px-4 py-2.5 text-right">Actions</th>}
              </tr>
            </thead>
            <tbody style={{ background: 'var(--surface-0)' }}>
              {rows.map(row => {
                const busy = (baseline.isPending && baseline.variables === row.model) ||
                  (reset.isPending && reset.variables === row.model)
                return (
                <tr key={row.model} className="border-t" style={{ borderColor: 'var(--border-sm)' }}>
                  <td className="px-4 py-3 font-mono font-semibold text-xs">{row.model}</td>
                  <td className="px-4 py-3 font-mono text-xs">{row.live_norm_mean.toFixed(4)}</td>
                  <td className="px-4 py-3 font-mono text-xs">{row.live_emb_mean.toFixed(4)}</td>
                  <td className="px-4 py-3 font-mono text-xs">{row.live_emb_std.toFixed(4)}</td>
                  <td className="px-4 py-3 font-mono text-xs">{row.max_z.toFixed(2)}</td>
                  <td className="px-4 py-3 text-xs font-semibold" style={statusStyle(row.status)}>
                    {row.status}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-muted-foreground">{row.n_snapshots}</td>
                  {admin && (
                    <td className="px-4 py-3 text-right whitespace-nowrap">
                      <button onClick={() => runBaseline(row.model)} disabled={busy} title="Set current embedding stats as baseline"
                        className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium mr-1.5 disabled:opacity-50"
                        style={{ background: 'oklch(0.64 0.20 265 / 12%)', border: '1px solid oklch(0.64 0.20 265 / 25%)', color: 'var(--accent-text)' }}>
                        <Target className="w-3 h-3" /> Baseline
                      </button>
                      <button onClick={() => runReset(row.model)} disabled={busy} title="Clear input snapshots"
                        className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium disabled:opacity-50"
                        style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
                        <Eraser className="w-3 h-3" /> Reset
                      </button>
                    </td>
                  )}
                </tr>
              )})}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function AutoRetrainTab({ onRefresh }: { onRefresh: () => void }) {
  const { data: rows = [], isLoading, error } = useQuery<AutoRetrain[]>({
    queryKey: ['drift-auto-retrain'],
    queryFn: () => apiFetch<AutoRetrain[]>('/api/drift/auto-retrain'),
  })
  const admin = isAdmin()
  const setAR = useSetAutoRetrain()

  const toggle = async (row: AutoRetrain) => {
    if (row.enabled) {
      await setAR.mutateAsync({ model: row.model, body: { enabled: false } })
      return
    }
    let dataset = row.dataset_name
    if (!dataset) {
      dataset = window.prompt(`Dataset to train ${row.model} on when drift fires?`) || ''
      if (!dataset) return
    }
    await setAR.mutateAsync({
      model: row.model,
      body: { enabled: true, dataset, minZ: row.min_z_score, cooldown: row.cooldown_s },
    })
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">Auto-retrain configuration per model.</p>
        <button
          onClick={onRefresh}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs"
          style={{ background: 'var(--surface-1)', color: 'var(--text-2)', border: '1px solid var(--border)' }}
        >
          <RefreshCw size={12} /> Refresh
        </button>
      </div>

      {error && (
        <p className="text-sm rounded-lg px-4 py-3"
          style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
          Failed to load auto-retrain config.
        </p>
      )}

      {isLoading && <TableSkeleton />}

      {!isLoading && !error && rows.length === 0 && (
        <EmptyState
          icon={Activity}
          title="No auto-retrain rules configured"
          description="Run `exa drift auto-retrain enable <MODEL> --dataset <DATASET>` to enable."
        />
      )}

      {rows.length > 0 && (
        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
          <table className="w-full text-sm">
            <thead style={{ background: 'var(--surface-1)' }}>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="px-4 py-2.5">Model</th>
                <th className="px-4 py-2.5">Enabled</th>
                <th className="px-4 py-2.5">Min Z-score</th>
                <th className="px-4 py-2.5">Dataset</th>
                <th className="px-4 py-2.5">Cooldown (s)</th>
                <th className="px-4 py-2.5">Last Triggered</th>
                {admin && <th className="px-4 py-2.5 text-right">Actions</th>}
              </tr>
            </thead>
            <tbody style={{ background: 'var(--surface-0)' }}>
              {rows.map(row => {
                const busy = setAR.isPending && setAR.variables?.model === row.model
                return (
                <tr key={row.model} className="border-t" style={{ borderColor: 'var(--border-sm)' }}>
                  <td className="px-4 py-3 font-mono font-semibold text-xs">{row.model}</td>
                  <td className="px-4 py-3 text-xs">
                    <span
                      className="px-2 py-0.5 rounded-full text-xs font-medium"
                      style={row.enabled
                        ? { background: 'oklch(0.72 0.18 155 / 12%)', border: '1px solid oklch(0.72 0.18 155 / 30%)', color: 'var(--success-text)' }
                        : { background: 'var(--surface-2)', border: '1px solid var(--border)', color: 'var(--muted-foreground)' }
                      }
                    >
                      {row.enabled ? 'enabled' : 'disabled'}
                    </span>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">{row.min_z_score.toFixed(1)}</td>
                  <td className="px-4 py-3 font-mono text-xs text-muted-foreground">{row.dataset_name}</td>
                  <td className="px-4 py-3 font-mono text-xs">{row.cooldown_s}</td>
                  <td className="px-4 py-3 text-xs text-muted-foreground">
                    {row.last_triggered ? new Date(row.last_triggered).toLocaleString() : '—'}
                  </td>
                  {admin && (
                    <td className="px-4 py-3 text-right whitespace-nowrap">
                      <button onClick={() => toggle(row)} disabled={busy}
                        title={row.enabled ? 'Disable auto-retrain' : 'Enable auto-retrain'}
                        className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium disabled:opacity-50"
                        style={row.enabled
                          ? { background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }
                          : { background: 'oklch(0.72 0.18 155 / 12%)', border: '1px solid oklch(0.72 0.18 155 / 30%)', color: 'var(--success-text)' }}>
                        <Power className="w-3 h-3" /> {busy ? '…' : row.enabled ? 'Disable' : 'Enable'}
                      </button>
                    </td>
                  )}
                </tr>
              )})}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

type DriftTab = 'prediction' | 'input' | 'auto-retrain'

export function Drift() {
  const [activeTab, setActiveTab] = useState<DriftTab>('prediction')
  const qc = useQueryClient()

  const tabs: { id: DriftTab; label: string }[] = [
    { id: 'prediction', label: 'Prediction Drift' },
    { id: 'input', label: 'Input Drift' },
    { id: 'auto-retrain', label: 'Auto-Retrain' },
  ]

  // Refetch the three drift queries so the visible tables update on demand.
  const handleRefresh = () => {
    qc.invalidateQueries({ queryKey: ['drift-status'] })
    qc.invalidateQueries({ queryKey: ['drift-input-status'] })
    qc.invalidateQueries({ queryKey: ['drift-auto-retrain'] })
  }

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="flex items-center gap-3">
        <div className="w-9 h-9 rounded-lg flex items-center justify-center"
          style={{ background: 'oklch(0.64 0.20 265 / 15%)', border: '1px solid oklch(0.64 0.20 265 / 30%)' }}>
          <Activity className="w-4 h-4" style={{ color: 'var(--accent-text)' }} />
        </div>
        <div>
          <h1 className="text-2xl font-bold">Drift Monitoring</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Prediction and input embedding drift detection.
          </p>
        </div>
      </div>

      {/* Tab bar */}
      <div className="flex gap-1 border-b" style={{ borderColor: 'var(--border)' }}>
        {tabs.map(({ id, label }) => (
          <button
            key={id}
            onClick={() => setActiveTab(id)}
            className="px-4 py-2 text-sm font-medium transition-colors"
            style={activeTab === id
              ? { borderBottom: '2px solid oklch(0.64 0.20 265)', color: 'oklch(0.64 0.20 265)' }
              : { color: 'var(--muted-foreground)' }}
          >
            {label}
          </button>
        ))}
      </div>

      {activeTab === 'prediction' && <PredictionDriftTab onRefresh={handleRefresh} />}
      {activeTab === 'input' && <InputDriftTab onRefresh={handleRefresh} />}
      {activeTab === 'auto-retrain' && <AutoRetrainTab onRefresh={handleRefresh} />}
    </div>
  )
}
