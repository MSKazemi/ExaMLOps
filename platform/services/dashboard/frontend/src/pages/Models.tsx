import { useState } from 'react'
import { RefreshCw, Package, AlertCircle, CheckCircle2, Hash, GitCommit, Layers, Radio, ChevronRight, ExternalLink, GitBranch, Settings2, PlusCircle } from 'lucide-react'
import { useNavigate, Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { useModels, useReloadRay, useModelRegistry, useModelzooStats, useModelzooModels, useHealth, useModelzooFreshness, useModelzooEvents, useMe, type ModelRegistryItem, type ModelzooModel, type ModelFreshness } from '@/lib/api'
import { FreshnessBadge } from '@/components/FreshnessBadge'
import { ScaffoldWizard } from '@/components/ScaffoldWizard'

function ModelCard({ name, version, runId, status, rayServeUrl }: {
  name: string
  version: string | null
  runId: string | null
  status: string
  rayServeUrl?: string
}) {
  const isOk = status === 'ok'
  const docsUrl = rayServeUrl ? `${rayServeUrl}/docs` : undefined

  return (
    <div
      onClick={() => docsUrl && window.open(docsUrl, '_blank', 'noopener,noreferrer')}
      className={`rounded-xl overflow-hidden${docsUrl ? ' cursor-pointer card-hover' : ''}`}
      style={{
        background: isOk ? 'oklch(0.72 0.18 155 / 5%)' : 'var(--surface-0)',
        border: isOk ? '1px solid oklch(0.72 0.18 155 / 30%)' : '1px solid oklch(0.66 0.22 25 / 30%)',
      }}
    >
      {/* Gradient top bar — green for live, red for error */}
      <div
        className="h-1"
        style={{
          background: isOk
            ? 'linear-gradient(90deg, oklch(0.72 0.18 155), oklch(0.65 0.20 160))'
            : 'linear-gradient(90deg, oklch(0.66 0.22 25), oklch(0.68 0.19 35))',
        }}
      />

      <div className="p-4 space-y-4">
        {/* Header */}
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2.5">
            <div
              className="w-8 h-8 rounded-md flex items-center justify-center shrink-0"
              style={{
                background: isOk ? 'oklch(0.72 0.18 155 / 15%)' : 'oklch(0.66 0.22 25 / 12%)',
                border: isOk ? '1px solid oklch(0.72 0.18 155 / 30%)' : '1px solid oklch(0.66 0.22 25 / 25%)',
              }}
            >
              <Radio className="w-3.5 h-3.5" style={{ color: isOk ? 'var(--success-text)' : 'var(--error-text)' }} />
            </div>
            <span className="font-semibold text-sm leading-tight">{name}</span>
          </div>
          {/* Live status badge with pulsing dot */}
          <div
            className="flex items-center gap-1.5 rounded-full px-2 py-0.5 shrink-0"
            style={isOk ? {
              background: 'oklch(0.72 0.18 155 / 15%)',
              border: '1px solid oklch(0.72 0.18 155 / 30%)',
            } : {
              background: 'oklch(0.66 0.22 25 / 12%)',
              border: '1px solid oklch(0.66 0.22 25 / 25%)',
            }}
          >
            {isOk ? (
              <span className="relative flex h-1.5 w-1.5">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-emerald-500" />
              </span>
            ) : (
              <AlertCircle className="w-3 h-3" style={{ color: 'var(--error-text)' }} />
            )}
            <span className="text-[10px] font-semibold" style={{ color: isOk ? 'var(--success-text)' : 'var(--error-text)' }}>
              {isOk ? 'LIVE' : status}
            </span>
          </div>
        </div>

        {/* Metadata */}
        <div className="space-y-1.5">
          <div className="flex items-center justify-between text-xs">
            <div className="flex items-center gap-1.5 text-muted-foreground">
              <Hash className="w-3 h-3" />
              <span>Version</span>
            </div>
            <span className="font-mono text-foreground/80">{version ?? '—'}</span>
          </div>
          <div className="flex items-center justify-between text-xs">
            <div className="flex items-center gap-1.5 text-muted-foreground">
              <GitCommit className="w-3 h-3" />
              <span>Run ID</span>
            </div>
            <span className="font-mono text-foreground/80 truncate max-w-[120px]">
              {runId ? runId.slice(0, 12) + '…' : '—'}
            </span>
          </div>
        </div>
      </div>
    </div>
  )
}

function RegistryCard({ item, freshness }: { item: ModelRegistryItem; freshness?: ModelFreshness }) {
  const navigate = useNavigate()
  return (
    <div
      onClick={() => navigate(`/models/${item.name}`)}
      className="rounded-xl overflow-hidden cursor-pointer card-hover"
      style={{
        background: 'var(--surface-0)',
        border: '1px solid oklch(0.64 0.20 265 / 25%)',
      }}
    >
      <div className="h-1" style={{ background: 'linear-gradient(90deg, oklch(0.64 0.20 265), oklch(0.70 0.18 300))' }} />
      <div className="p-4 space-y-3">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-md flex items-center justify-center shrink-0"
              style={{ background: 'oklch(0.64 0.20 265 / 14%)', border: '1px solid oklch(0.64 0.20 265 / 25%)' }}>
              <Package className="w-3.5 h-3.5" style={{ color: 'var(--accent-text)' }} />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="font-semibold text-sm">{item.name}</span>
                <FreshnessBadge status={freshness?.status ?? 'unknown'} staleSince={freshness?.stale_since} />
              </div>
              <div className="flex items-center gap-1 mt-0.5">
                <span className="text-[9px] font-mono px-1.5 py-0.5 rounded"
                  style={{ background: 'oklch(0.64 0.20 265 / 10%)', border: '1px solid oklch(0.64 0.20 265 / 20%)', color: 'var(--accent-text)' }}>
                  {item.task_type}
                </span>
              </div>
            </div>
          </div>
          <ChevronRight className="w-4 h-4 shrink-0" style={{ color: 'var(--faint-text)' }} />
        </div>
        <div className="flex flex-wrap gap-1">
          {item.supported_datasets.map(d => (
            <span key={d} className="text-[10px] px-1.5 py-0.5 rounded-md font-mono"
              style={{ background: 'var(--surface-2)', border: '1px solid var(--border-sm)', color: 'var(--subtle-text)' }}>
              {d}
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}

function GitLabModelCard({ model }: { model: ModelzooModel }) {
  const navigate = useNavigate()
  const label = model.task_category.replace(/_/g, ' ')
  return (
    <div
      className="rounded-xl overflow-hidden cursor-pointer card-hover"
      style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}
      onClick={() => navigate(`/models/${model.name.toUpperCase()}`)}
    >
      <div className="h-0.5" style={{ background: 'linear-gradient(90deg, oklch(0.78 0.18 80), oklch(0.64 0.20 265))' }} />
      <div className="p-4 space-y-2">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-md flex items-center justify-center shrink-0"
              style={{ background: 'oklch(0.78 0.18 80 / 12%)', border: '1px solid oklch(0.78 0.18 80 / 25%)' }}>
              <Package className="w-3.5 h-3.5" style={{ color: 'var(--warning-text)' }} />
            </div>
            <span className="font-semibold text-sm font-mono">{model.name}</span>
          </div>
          {model.file_url && (
            <a
              href={model.file_url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={e => e.stopPropagation()}
              title="View in GitLab"
              className="p-1.5 rounded-md text-muted-foreground hover:text-foreground transition-colors shrink-0"
            >
              <ExternalLink className="w-3.5 h-3.5" />
            </a>
          )}
        </div>
        <span className="text-[10px] font-mono px-2 py-0.5 rounded-md inline-block"
          style={{ background: 'var(--surface-2)', border: '1px solid var(--border-sm)', color: 'var(--subtle-text)' }}>
          {label}
        </span>
      </div>
    </div>
  )
}

export function Models() {
  const qc = useQueryClient()
  const [mzSyncing, setMzSyncing] = useState(false)
  const [showScaffold, setShowScaffold] = useState(false)
  const { data: me } = useMe()
  const isAdmin = me?.role === 'admin'

  const handleMzSync = async () => {
    setMzSyncing(true)
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['modelzoo', 'models'] }),
      qc.invalidateQueries({ queryKey: ['modelzoo', 'stats'] }),
    ])
    setMzSyncing(false)
  }

  const { data: models, isLoading, error, refetch } = useModels()
  const { data: registry, isLoading: registryLoading } = useModelRegistry()
  const { data: zoo } = useModelzooStats()
  const { data: zooModels, isLoading: zooLoading } = useModelzooModels()
  const { data: health } = useHealth()
  const { data: freshnessData } = useModelzooFreshness()
  const freshnessMap = Object.fromEntries(
    (freshnessData?.models ?? []).map(m => [m.model_id, m])
  )
  const { data: mzEvents } = useModelzooEvents(5)
  const reload = useReloadRay()
  const rayServeUrl = health?.services?.ray_serve?.url

  const handleReload = async () => {
    await reload.mutateAsync()
    refetch()
  }

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Models</h1>
          <p className="text-muted-foreground text-sm mt-1">
            MLflow registry and live Ray Serve deployments.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {isAdmin && (
            <button
              onClick={() => setShowScaffold(true)}
              className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium"
              style={{ background: 'oklch(0.72 0.18 155 / 15%)',
                       color: 'oklch(0.72 0.18 155)',
                       border: '1px solid oklch(0.72 0.18 155 / 30%)' }}
            >
              <PlusCircle size={14} />
              New Model
            </button>
          )}
          {showScaffold && <ScaffoldWizard onClose={() => setShowScaffold(false)} />}
          <button
            onClick={handleMzSync}
            disabled={mzSyncing}
            className="inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-150 disabled:opacity-50 disabled:cursor-not-allowed"
            style={{
              background: 'var(--surface-1)',
              border: '1px solid var(--border)',
              color: 'var(--foreground)',
            }}
          >
            <RefreshCw className={`w-3.5 h-3.5 ${mzSyncing ? 'animate-spin' : ''}`} />
            {mzSyncing ? 'Syncing…' : 'Sync from GitLab'}
          </button>
          <button
            onClick={handleReload}
            disabled={reload.isPending}
            className="inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-150 disabled:opacity-50 disabled:cursor-not-allowed glow-primary"
            style={{
              background: 'oklch(0.64 0.20 265)',
              color: 'oklch(0.99 0 0)',
              border: '1px solid oklch(0.64 0.20 265 / 60%)',
            }}
          >
            <RefreshCw className={`w-3.5 h-3.5 ${reload.isPending ? 'animate-spin' : ''}`} />
            {reload.isPending ? 'Reloading…' : 'Reload Ray Serve'}
          </button>
        </div>
      </div>

      {reload.isSuccess && (
        <Alert style={{
          background: 'oklch(0.72 0.18 155 / 10%)',
          border: '1px solid oklch(0.72 0.18 155 / 25%)',
        }}>
          <CheckCircle2 className="h-4 w-4" style={{ color: 'var(--success-text)' }} />
          <AlertDescription style={{ color: 'var(--success-text)' }}>
            Ray Serve reloaded — {reload.data.count} model(s) loaded.
          </AlertDescription>
        </Alert>
      )}

      {/* GitLab ModelZoo section */}
      <div className="space-y-3">
        <div
          className="rounded-xl p-4 flex items-start gap-3"
          style={{
            background: 'oklch(0.78 0.18 80 / 6%)',
            border: '1px solid oklch(0.78 0.18 80 / 20%)',
          }}
        >
          <div
            className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0 mt-0.5"
            style={{ background: 'oklch(0.78 0.18 80 / 15%)', border: '1px solid oklch(0.78 0.18 80 / 30%)' }}
          >
            <GitBranch className="w-4 h-4" style={{ color: 'var(--warning-text)' }} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h2 className="text-sm font-semibold">GitLab ModelZoo</h2>
              {zooModels && zooModels.length > 0 && (
                <span className="text-[10px] px-1.5 py-0.5 rounded font-medium"
                  style={{ background: 'oklch(0.78 0.18 80 / 15%)', color: 'var(--warning-text)' }}>
                  {zooModels.length} model{zooModels.length !== 1 ? 's' : ''} in repo
                </span>
              )}
              {zoo?.repo_url && (
                <a href={zoo.repo_url} target="_blank" rel="noopener noreferrer"
                  className="ml-auto text-[10px] text-muted-foreground flex items-center gap-1 hover:opacity-80">
                  <ExternalLink className="w-3 h-3" /> view repo
                </a>
              )}
            </div>
            <p className="text-xs text-muted-foreground mt-0.5">
              Auto-discovered from the repository. Click a card to go to its detail page,
              or the <ExternalLink className="w-3 h-3 inline" /> icon to open it in GitLab.
            </p>
            {!zoo?.configured && (
              <p className="text-xs mt-1" style={{ color: 'var(--warning-text)' }}>
                GitLab not configured —{' '}
                <Link to="/config" className="underline underline-offset-2 hover:opacity-80">
                  add project ID and token in Config <Settings2 className="w-3 h-3 inline" />
                </Link>
              </p>
            )}
          </div>
        </div>

        {zoo?.configured && zooLoading && (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-24 rounded-xl animate-pulse"
                style={{ background: 'var(--surface-1)', border: '1px solid var(--border-sm)' }} />
            ))}
          </div>
        )}

        {zoo?.configured && !zooLoading && zooModels && zooModels.length > 0 && (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {zooModels.map(m => <GitLabModelCard key={m.name} model={m} />)}
          </div>
        )}
      </div>

      {/* Model Registry section */}
      <div className="space-y-3">
        <div
          className="rounded-xl p-4 flex items-start gap-3"
          style={{
            background: 'oklch(0.64 0.20 265 / 6%)',
            border: '1px solid oklch(0.64 0.20 265 / 20%)',
          }}
        >
          <div
            className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0 mt-0.5"
            style={{ background: 'oklch(0.64 0.20 265 / 15%)', border: '1px solid oklch(0.64 0.20 265 / 30%)' }}
          >
            <Layers className="w-4 h-4" style={{ color: 'var(--accent-text)' }} />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold">Model Registry</h2>
              {registry && <span className="text-[10px] px-1.5 py-0.5 rounded font-medium" style={{ background: 'oklch(0.64 0.20 265 / 15%)', color: 'var(--accent-text)' }}>{registry.length} model{registry.length !== 1 ? 's' : ''}</span>}
            </div>
            <p className="text-xs text-muted-foreground mt-0.5">
              Models trained and versioned in MLflow. These are <span className="font-medium" style={{ color: 'var(--foreground)' }}>not necessarily live</span> — they may be in Staging, Canary, or archived. Click any card to explore versions and run inference.
            </p>
          </div>
        </div>

        {registryLoading && (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-24 rounded-xl animate-pulse"
                style={{ background: 'var(--surface-1)', border: '1px solid var(--border-sm)' }} />
            ))}
          </div>
        )}

        {!registryLoading && registry && registry.length > 0 && (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {registry.map(item => (
              <RegistryCard key={item.name} item={item} freshness={freshnessMap[item.name]} />
            ))}
          </div>
        )}

        {!registryLoading && registry?.length === 0 && (
          <div
            className="rounded-xl p-6 text-center"
            style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}
          >
            <p className="text-sm text-muted-foreground">No models registered.</p>
          </div>
        )}
      </div>

      {/* Ray Serve live section header */}
      <div
        className="rounded-xl p-4 flex items-start gap-3"
        style={{
          background: 'oklch(0.72 0.18 155 / 6%)',
          border: '1px solid oklch(0.72 0.18 155 / 25%)',
        }}
      >
        <div
          className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0 mt-0.5"
          style={{ background: 'oklch(0.72 0.18 155 / 15%)', border: '1px solid oklch(0.72 0.18 155 / 30%)' }}
        >
          <Radio className="w-4 h-4" style={{ color: 'var(--success-text)' }} />
        </div>
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-semibold">Ray Serve</h2>
            <div
              className="flex items-center gap-1.5 rounded-full px-2 py-0.5"
              style={{ background: 'oklch(0.72 0.18 155 / 15%)', border: '1px solid oklch(0.72 0.18 155 / 30%)' }}
            >
              <span className="relative flex h-1.5 w-1.5">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-emerald-500" />
              </span>
              <span className="text-[10px] font-semibold" style={{ color: 'var(--success-text)' }}>LIVE</span>
            </div>
            {models && <span className="text-[10px] px-1.5 py-0.5 rounded font-medium" style={{ background: 'oklch(0.72 0.18 155 / 15%)', color: 'var(--success-text)' }}>{models.length} deployment{models.length !== 1 ? 's' : ''}</span>}
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">
            Models <span className="font-medium" style={{ color: 'var(--success-text)' }}>actively handling inference requests</span> right now via Ray Serve. Each entry is a loaded deployment with a specific version and run.
          </p>
        </div>
      </div>

      {error && (
        <p className="text-sm rounded-lg px-4 py-3"
          style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
          Failed to load models from Ray Serve.
        </p>
      )}

      {isLoading && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="h-32 rounded-xl animate-pulse"
              style={{ background: 'var(--surface-1)', border: '1px solid var(--border-sm)' }} />
          ))}
        </div>
      )}

      {!isLoading && models?.length === 0 && (
        <div
          className="rounded-xl p-8 text-center"
          style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}
        >
          <Package className="w-8 h-8 text-muted-foreground/40 mx-auto mb-3" />
          <p className="text-sm text-muted-foreground">No models currently loaded.</p>
          <p className="text-xs text-muted-foreground/60 mt-1">
            Run a pipeline to promote a model to Production, then reload Ray Serve.
          </p>
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {models?.map((m) => (
          <ModelCard
            key={m.model_name}
            name={m.model_name}
            version={m.model_version}
            runId={m.run_id}
            status={m.status}
            rayServeUrl={rayServeUrl}
          />
        ))}
      </div>

      {mzEvents && mzEvents.length > 0 && (
        <div className="rounded-xl overflow-hidden" style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}>
          <div className="px-4 py-3 border-b" style={{ borderColor: 'var(--border)' }}>
            <h2 className="text-sm font-semibold">Recent ModelZoo Pushes</h2>
          </div>
          <div className="divide-y" style={{ borderColor: 'var(--border)' }}>
            {mzEvents.map(ev => (
              <div key={ev.id} className="px-4 py-2.5 flex items-center gap-3 text-xs">
                <span className="font-mono text-muted-foreground">{ev.commit_sha.slice(0, 8)}</span>
                <span>{ev.pushed_by ?? '—'}</span>
                <span className="text-muted-foreground">{String(ev.timestamp).slice(0, 19).replace('T', ' ')}</span>
                <span className="ml-auto text-muted-foreground">{ev.source}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
