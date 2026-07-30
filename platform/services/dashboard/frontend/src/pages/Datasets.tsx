import { useState, useEffect } from 'react'
import { ExternalLink, Database, AlertCircle, Settings2, Box, RefreshCw, Play } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import { useModelzooDatasets, useModelzooStats, useModelRegistry, triggerPipeline, getPipelineStatus } from '@/lib/api'
import type { ModelzooDataset } from '@/lib/api'
import { EmptyState } from '@/components/ui/empty-state'
import { Skeleton } from '@/components/ui/skeleton'
import { Link } from 'react-router-dom'
import { isAdmin } from '@/lib/auth'

function NotConfiguredBanner() {
  return (
    <div
      className="rounded-xl p-6 flex items-start gap-4"
      style={{
        background: 'oklch(0.78 0.18 80 / 8%)',
        border: '1px solid oklch(0.78 0.18 80 / 25%)',
      }}
    >
      <AlertCircle className="w-5 h-5 shrink-0 mt-0.5" style={{ color: 'var(--warning-text)' }} />
      <div className="space-y-1">
        <p className="text-sm font-medium" style={{ color: 'var(--warning-text)' }}>
          GitLab integration not configured
        </p>
        <p className="text-xs text-muted-foreground">
          Add your <strong>GitLab Project ID</strong> and <strong>GitLab Token</strong> in{' '}
          <Link to="/platform/config" className="underline underline-offset-2 hover:opacity-80">
            Config <Settings2 className="w-3 h-3 inline" />
          </Link>{' '}
          to auto-discover datasets from the modelzoo repository.
        </p>
      </div>
    </div>
  )
}

function DatasetCard({
  dataset,
  usedByModels,
}: {
  dataset: ModelzooDataset
  usedByModels: string[]
}) {
  return (
    <div
      className="rounded-xl overflow-hidden"
      style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}
    >
      <div
        className="h-0.5"
        style={{ background: 'linear-gradient(90deg, oklch(0.64 0.20 265), oklch(0.72 0.18 155))' }}
      />
      <div className="p-4 space-y-3">
        {/* Header */}
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2.5">
            <div
              className="w-8 h-8 rounded-md flex items-center justify-center shrink-0"
              style={{
                background: 'oklch(0.72 0.18 155 / 12%)',
                border: '1px solid oklch(0.72 0.18 155 / 25%)',
              }}
            >
              <Database className="w-3.5 h-3.5" style={{ color: 'var(--success-text)' }} />
            </div>
            <div>
              <p className="font-semibold text-sm leading-tight">{dataset.class_name}</p>
              <p className="text-[10px] font-mono text-muted-foreground mt-0.5">{dataset.filename}</p>
            </div>
          </div>
          {dataset.file_url && (
            <a
              href={dataset.file_url}
              target="_blank"
              rel="noopener noreferrer"
              title="View in GitLab"
              className="p-1.5 rounded-md text-muted-foreground hover:text-foreground transition-colors shrink-0"
            >
              <ExternalLink className="w-3.5 h-3.5" />
            </a>
          )}
        </div>

        {/* Used by models */}
        {usedByModels.length > 0 ? (
          <div className="space-y-1">
            <p className="text-[10px] font-medium text-muted-foreground uppercase tracking-wide">
              Used by
            </p>
            <div className="flex flex-wrap gap-1.5">
              {usedByModels.map(name => (
                <Link
                  key={name}
                  to={`/models/${name}`}
                  className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-mono hover:opacity-80 transition-opacity"
                  style={{
                    background: 'oklch(0.64 0.20 265 / 12%)',
                    border: '1px solid oklch(0.64 0.20 265 / 25%)',
                    color: 'var(--accent-text)',
                  }}
                >
                  <Box className="w-2.5 h-2.5" />
                  {name}
                </Link>
              ))}
            </div>
          </div>
        ) : (
          <p className="text-[10px] text-muted-foreground/50 italic">
            No registered models use this dataset yet
          </p>
        )}
      </div>
    </div>
  )
}

const TERMINAL_STATUSES = new Set(['success', 'failed', 'canceled', 'timeout'])

function pipelineStatusColor(status: string): string {
  if (status === 'success') return 'var(--success-text)'
  if (status === 'failed' || status === 'canceled') return 'var(--error-text)'
  return 'var(--accent-text)'
}

function pipelineStatusBg(status: string): string {
  if (status === 'success') return 'oklch(0.72 0.18 155 / 12%)'
  if (status === 'failed' || status === 'canceled') return 'oklch(0.66 0.22 25 / 12%)'
  return 'oklch(0.64 0.20 265 / 12%)'
}

function pipelineStatusBorder(status: string): string {
  if (status === 'success') return 'oklch(0.72 0.18 155 / 30%)'
  if (status === 'failed' || status === 'canceled') return 'oklch(0.66 0.22 25 / 30%)'
  return 'oklch(0.64 0.20 265 / 30%)'
}

export function Datasets() {
  const qc = useQueryClient()
  const [syncing, setSyncing] = useState(false)

  // CI pipeline trigger state
  const [pipelineId, setPipelineId] = useState<number | null>(null)
  const [pipelineStatus, setPipelineStatus] = useState<string | null>(null)
  const [pipelineUrl, setPipelineUrl] = useState<string | null>(null)
  const [triggering, setTriggering] = useState(false)
  const [triggerError, setTriggerError] = useState<string | null>(null)

  const handleTrigger = async () => {
    setTriggering(true)
    setTriggerError(null)
    try {
      const result = await triggerPipeline()
      setPipelineId(result.pipeline_id)
      setPipelineStatus(result.status)
      setPipelineUrl(result.web_url)
    } catch (err) {
      setTriggerError(err instanceof Error ? err.message : 'Failed to trigger pipeline')
    } finally {
      setTriggering(false)
    }
  }

  // Poll pipeline status while it is in a non-terminal state
  useEffect(() => {
    if (pipelineId === null) return
    if (pipelineStatus !== null && TERMINAL_STATUSES.has(pipelineStatus)) return

    let count = 0
    let failCount = 0

    const interval = setInterval(async () => {
      try {
        const result = await getPipelineStatus(pipelineId)
        failCount = 0
        setPipelineStatus(result.status)
        setPipelineUrl(result.web_url)
        if (TERMINAL_STATUSES.has(result.status)) {
          clearInterval(interval)
          return
        }
        count++
        if (count >= 60) {
          setTriggerError('Pipeline timed out after 5 minutes')
          setPipelineStatus('timeout')
          clearInterval(interval)
        }
      } catch {
        failCount++
        if (failCount >= 3) {
          setTriggerError('Lost contact with pipeline status — click Run to retry')
          setPipelineStatus('failed')
          clearInterval(interval)
        }
      }
    }, 5_000)

    return () => clearInterval(interval)
    // Intentionally keyed on pipelineId only: including pipelineStatus would tear
    // down and recreate this interval (resetting `count` to 0) on every status
    // transition, so the `count >= 60` 5-minute cap could never fire. Termination
    // is driven by the interval's own `result.status` check instead.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipelineId])

  const handleSync = async () => {
    setSyncing(true)
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['modelzoo', 'datasets'] }),
      qc.invalidateQueries({ queryKey: ['modelzoo', 'stats'] }),
    ])
    setSyncing(false)
  }

  const { data: stats, isLoading: statsLoading } = useModelzooStats()
  const { data: datasets, isLoading: datasetsLoading, isFetching, error } = useModelzooDatasets()
  const { data: registry } = useModelRegistry()

  const isLoading = statsLoading || datasetsLoading

  // Build lookup: dataset class_name → list of model names that use it
  const datasetToModels: Record<string, string[]> = {}
  if (registry) {
    for (const model of registry) {
      for (const ds of model.supported_datasets) {
        if (!datasetToModels[ds]) datasetToModels[ds] = []
        datasetToModels[ds].push(model.name)
      }
    }
  }

  const notConfigured = stats && !stats.configured

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      {/* ── Header ── */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Datasets</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Auto-discovered from the modelzoo GitLab repository.
          </p>
        </div>

        <div className="flex items-center gap-3 shrink-0">
          {stats?.configured && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span
                className="w-1.5 h-1.5 rounded-full inline-block"
                style={{ background: 'var(--success-text)' }}
              />
              {stats.branch} branch
              {stats.repo_url && (
                <a
                  href={stats.repo_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 hover:opacity-80"
                >
                  <ExternalLink className="w-3 h-3" />
                  repo
                </a>
              )}
            </div>
          )}
          <button
            onClick={handleSync}
            disabled={syncing || isFetching}
            className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-all duration-150 disabled:opacity-50 disabled:cursor-not-allowed"
            style={{
              background: 'var(--surface-1)',
              border: '1px solid var(--border)',
              color: 'var(--foreground)',
            }}
          >
            <RefreshCw className={`w-3.5 h-3.5 ${syncing || isFetching ? 'animate-spin' : ''}`} />
            {syncing ? 'Syncing…' : 'Sync from GitLab'}
          </button>
        </div>
      </div>

      {/* ── CI Pipeline trigger (admins only) ── */}
      {isAdmin() && (
        <div
          className="rounded-xl p-4 space-y-3"
          style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}
        >
          <div className="flex items-center justify-between gap-4 flex-wrap">
            <div>
              <p className="text-sm font-semibold">CI Pipeline</p>
              <p className="text-xs text-muted-foreground mt-0.5">
                Trigger the GitLab modelzoo CI pipeline to re-run checks and training.
              </p>
            </div>
            <div className="flex items-center gap-3 shrink-0 flex-wrap">
              {pipelineStatus && (
                <div className="flex items-center gap-2">
                  <span
                    className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium"
                    style={{
                      background: pipelineStatusBg(pipelineStatus),
                      border: `1px solid ${pipelineStatusBorder(pipelineStatus)}`,
                      color: pipelineStatusColor(pipelineStatus),
                    }}
                  >
                    {!TERMINAL_STATUSES.has(pipelineStatus) && (
                      <span className="w-1.5 h-1.5 rounded-full animate-pulse inline-block"
                        style={{ background: pipelineStatusColor(pipelineStatus) }} />
                    )}
                    {pipelineStatus}
                  </span>
                  {pipelineUrl && (
                    <a
                      href={pipelineUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
                    >
                      <ExternalLink className="w-3 h-3" />
                      View
                    </a>
                  )}
                </div>
              )}
              <button
                onClick={handleTrigger}
                disabled={triggering || (pipelineStatus !== null && !TERMINAL_STATUSES.has(pipelineStatus))}
                className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-all duration-150 disabled:opacity-50 disabled:cursor-not-allowed"
                style={{
                  background: 'oklch(0.64 0.20 265 / 12%)',
                  border: '1px solid oklch(0.64 0.20 265 / 30%)',
                  color: 'var(--accent-text)',
                }}
              >
                <Play className="w-3.5 h-3.5" />
                {triggering ? 'Triggering…' : 'Run CI Pipeline'}
              </button>
            </div>
          </div>
          {triggerError && (
            <p
              className="text-xs rounded-md px-3 py-2"
              style={{
                background: 'oklch(0.66 0.22 25 / 10%)',
                border: '1px solid oklch(0.66 0.22 25 / 25%)',
                color: 'var(--error-text)',
              }}
            >
              {triggerError}
            </p>
          )}
        </div>
      )}

      {/* ── Not configured ── */}
      {notConfigured && <NotConfiguredBanner />}

      {/* ── Error ── */}
      {error && (
        <div
          className="rounded-xl px-4 py-3 text-sm"
          style={{
            background: 'oklch(0.66 0.22 25 / 10%)',
            border: '1px solid oklch(0.66 0.22 25 / 25%)',
            color: 'var(--error-text)',
          }}
        >
          Failed to load datasets from GitLab.
        </div>
      )}

      {/* ── Skeleton ── */}
      {isLoading && !notConfigured && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4" aria-label="Loading datasets">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-32 w-full" />
          ))}
        </div>
      )}

      {/* ── Dataset grid ── */}
      {!isLoading && datasets && datasets.length > 0 && (
        <>
          {/* Summary strip */}
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span
              className="font-semibold px-2 py-0.5 rounded-full"
              style={{
                background: 'oklch(0.72 0.18 155 / 12%)',
                border: '1px solid oklch(0.72 0.18 155 / 25%)',
                color: 'var(--success-text)',
              }}
            >
              {datasets.length} dataset{datasets.length !== 1 ? 's' : ''}
            </span>
            <span>discovered in repository</span>
            {stats?.last_commit && (
              <span className="ml-auto opacity-60">
                last commit {new Date(stats.last_commit.committed_date).toLocaleDateString()}
                {' · '}{stats.last_commit.author_name}
              </span>
            )}
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {datasets.map(ds => (
              <DatasetCard
                key={ds.class_name}
                dataset={ds}
                usedByModels={datasetToModels[ds.class_name] ?? []}
              />
            ))}
          </div>
        </>
      )}

      {/* ── Empty ── */}
      {!isLoading && datasets?.length === 0 && stats?.configured && (
        <EmptyState
          icon={Database}
          title="No datasets found"
          description="No dataset files were discovered in the repository."
        />
      )}
    </div>
  )
}
