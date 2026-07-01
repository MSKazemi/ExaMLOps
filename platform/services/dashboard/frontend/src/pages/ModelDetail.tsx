import { useState, useRef, useEffect } from 'react'
import { useParams, Link } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  ArrowLeft, ExternalLink, AlertTriangle, Tag,
  Cpu, BarChart3, Edit3, X, RotateCcw, Upload, Trash2, Send,
  ChevronDown, Clock, Layers,
} from 'lucide-react'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { useQuery } from '@tanstack/react-query'
import {
  apiFetch, useModelDetail, useUpdateDescription, useRevertDescription,
  useUploadImage, useDeleteImage, usePredict,
  useModelVersions, useSetAlias, useDeleteAlias, type ModelVersion,
} from '@/lib/api'
import { rewriteImageUrls } from '@/lib/markdown'
import { isAdmin } from '@/lib/auth'
import MDEditor from '@uiw/react-md-editor'
import '@uiw/react-md-editor/markdown-editor.css'

interface CostRow {
  version: number
  run_id: string | null
  job_id: string | null
  gpu_hours: number | null
  cost_usd: number | null
  recorded_at: string
}

const STATUS_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  stable:       { bg: 'oklch(0.72 0.18 155 / 12%)', border: 'oklch(0.72 0.18 155 / 30%)', text: 'var(--success-text)' },
  experimental: { bg: 'oklch(0.78 0.18 55 / 12%)',  border: 'oklch(0.78 0.18 55 / 30%)',  text: 'var(--warning-text)'  },
  deprecated:   { bg: 'oklch(0.66 0.22 25 / 12%)',  border: 'oklch(0.66 0.22 25 / 25%)',  text: 'var(--error-text)'              },
  archived:     { bg: 'var(--surface-2)',       border: 'var(--border-md)',          text: 'var(--faint-text)'},
}

function StageBadge({ label, stage }: { label: string; stage: { version: string; alias: string } | null }) {
  const isSet = stage !== null
  return (
    <div className="flex flex-col gap-1 items-center">
      <span className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</span>
      <div
        className="rounded-lg px-3 py-1.5 text-xs font-mono font-semibold"
        style={isSet ? {
          background: 'oklch(0.64 0.20 265 / 14%)',
          border: '1px solid oklch(0.64 0.20 265 / 30%)',
          color: 'var(--accent-text)',
        } : {
          background: 'var(--surface-1)',
          border: '1px solid var(--border-sm)',
          color: 'oklch(0.45 0.015 260)',
        }}
      >
        {stage ? `v${stage.version}` : '—'}
      </div>
    </div>
  )
}

function LinkButton({ label, url }: { label: string; url: string }) {
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors hover:opacity-80"
      style={{
        background: 'oklch(0.64 0.20 265 / 10%)',
        border: '1px solid oklch(0.64 0.20 265 / 22%)',
        color: 'var(--accent-text)',
      }}
    >
      <ExternalLink className="w-3 h-3" />
      {label.replace(/_/g, ' ')}
    </a>
  )
}

const ALIAS_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  Production: { bg: 'oklch(0.72 0.18 155 / 12%)', border: 'oklch(0.72 0.18 155 / 30%)', text: 'oklch(0.55 0.18 155)' },
  Canary:     { bg: 'oklch(0.80 0.18 80 / 12%)',  border: 'oklch(0.80 0.18 80 / 30%)',  text: 'oklch(0.62 0.18 80)' },
  Staging:    { bg: 'oklch(0.64 0.20 265 / 12%)', border: 'oklch(0.64 0.20 265 / 30%)', text: 'oklch(0.50 0.20 265)' },
  Archived:   { bg: 'oklch(0.60 0.00 0 / 10%)',   border: 'oklch(0.60 0.00 0 / 20%)',   text: 'oklch(0.50 0.00 0)' },
}

function AliasBadge({ alias }: { alias: string }) {
  const c = ALIAS_COLORS[alias] ?? ALIAS_COLORS.Archived
  return (
    <span
      className="px-2 py-0.5 rounded-full text-xs font-medium"
      style={{ background: c.bg, border: `1px solid ${c.border}`, color: c.text }}
    >
      {alias}
    </span>
  )
}

const VALID_ALIASES = ['Staging', 'Canary', 'Production', 'Archived'] as const

function VersionsTab({ modelName, admin }: { modelName: string; admin: boolean }) {
  const { data: versions, isLoading, isError } = useModelVersions(modelName)
  const setAlias = useSetAlias(modelName)
  const deleteAlias = useDeleteAlias(modelName)
  const [confirmAction, setConfirmAction] = useState<{
    version: string; alias: string; isDelete: boolean
  } | null>(null)
  const [mutationError, setMutationError] = useState<string | null>(null)
  const mutatingRef = useRef(false)

  if (isLoading) return <p className="text-sm text-muted-foreground p-4">Loading versions…</p>
  if (isError)   return <p className="text-sm text-red-500 p-4">Failed to load versions.</p>
  if (!versions?.length) return <p className="text-sm text-muted-foreground p-4">No versions registered yet.</p>

  const handleConfirm = async () => {
    if (!confirmAction || mutatingRef.current) return
    mutatingRef.current = true
    const action = confirmAction
    setConfirmAction(null)
    setMutationError(null)
    try {
      if (action.isDelete) {
        await deleteAlias.mutateAsync({ version: action.version, alias: action.alias })
      } else {
        await setAlias.mutateAsync({ version: action.version, alias: action.alias })
      }
    } catch (e) {
      setMutationError(e instanceof Error ? e.message : 'Operation failed')
    } finally {
      mutatingRef.current = false
    }
  }

  return (
    <div className="space-y-3">
      {mutationError && (
        <p className="text-xs text-red-500 px-1">{mutationError}</p>
      )}
      {/* Confirm dialog */}
      {confirmAction && (
        <div
          className="rounded-xl p-4 space-y-3"
          style={{ background: 'oklch(0.78 0.18 80 / 8%)', border: '1px solid oklch(0.78 0.18 80 / 25%)' }}
        >
          <p className="text-sm font-medium">
            {confirmAction.isDelete
              ? `Remove "${confirmAction.alias}" alias from v${confirmAction.version}?`
              : confirmAction.alias === 'Production'
              ? `Set v${confirmAction.version} as Production? The previous Production version will be moved to Archived.`
              : `Set "${confirmAction.alias}" alias on v${confirmAction.version}?`}
          </p>
          <div className="flex gap-2">
            <button
              onClick={handleConfirm}
              disabled={setAlias.isPending || deleteAlias.isPending}
              className="px-3 py-1.5 rounded-lg text-xs font-medium"
              style={{ background: 'oklch(0.64 0.20 265)', color: 'white' }}
            >
              {setAlias.isPending || deleteAlias.isPending ? 'Applying…' : 'Confirm'}
            </button>
            <button
              onClick={() => setConfirmAction(null)}
              className="px-3 py-1.5 rounded-lg text-xs font-medium"
              style={{ background: 'var(--surface-1)', border: '1px solid var(--border)' }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Version table */}
      <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
        <table className="w-full text-sm">
          <thead>
            <tr style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border)' }}>
              <th className="text-left px-4 py-2.5 text-xs font-semibold text-muted-foreground">Version</th>
              <th className="text-left px-4 py-2.5 text-xs font-semibold text-muted-foreground">Date</th>
              <th className="text-left px-4 py-2.5 text-xs font-semibold text-muted-foreground">Framework</th>
              <th className="text-left px-4 py-2.5 text-xs font-semibold text-muted-foreground">Aliases</th>
              <th className="text-left px-4 py-2.5 text-xs font-semibold text-muted-foreground">Metrics</th>
              {admin && <th className="text-left px-4 py-2.5 text-xs font-semibold text-muted-foreground">Actions</th>}
            </tr>
          </thead>
          <tbody>
            {[...versions].sort((a, b) => parseInt(b.version) - parseInt(a.version)).map((v: ModelVersion, i: number) => (
              <tr
                key={v.version}
                style={{
                  background: i % 2 === 0 ? 'var(--surface-0)' : 'var(--surface-1)',
                  borderBottom: '1px solid var(--border)',
                }}
              >
                <td className="px-4 py-3 font-mono text-xs">v{v.version}</td>
                <td className="px-4 py-3 text-xs text-muted-foreground">
                  {new Date(v.created_at).toLocaleDateString()}
                </td>
                <td className="px-4 py-3 text-xs">{v.framework}</td>
                <td className="px-4 py-3">
                  <div className="flex flex-wrap gap-1">
                    {v.aliases.length > 0
                      ? v.aliases.map(a => (
                          <div key={a} className="flex items-center gap-1">
                            <AliasBadge alias={a} />
                            {admin && a !== 'Archived' && (
                              <button
                                onClick={() => setConfirmAction({ version: v.version, alias: a, isDelete: true })}
                                className="opacity-40 hover:opacity-100 transition-opacity"
                                title={`Remove ${a} alias`}
                              >
                                <Trash2 className="w-3 h-3" />
                              </button>
                            )}
                          </div>
                        ))
                      : <span className="text-xs text-muted-foreground">—</span>}
                  </div>
                </td>
                <td className="px-4 py-3 text-xs font-mono text-muted-foreground">
                  {Object.entries(v.metrics).map(([k, val]) =>
                    `${k}: ${typeof val === 'number' ? val.toFixed(2) : val}`
                  ).join(' · ') || '—'}
                </td>
                {admin && (
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <div className="relative group">
                        <button
                          className="flex items-center gap-1 px-2 py-1 rounded text-xs font-medium"
                          style={{ background: 'var(--surface-1)', border: '1px solid var(--border)' }}
                        >
                          Promote <ChevronDown className="w-3 h-3" />
                        </button>
                        <div
                          className="absolute right-0 top-full mt-1 z-10 rounded-lg py-1 hidden group-focus-within:block group-hover:block shadow-lg min-w-[120px]"
                          style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}
                        >
                          {VALID_ALIASES.filter(a => a !== 'Archived' && !v.aliases.includes(a)).map(a => (
                            <button
                              key={a}
                              onClick={() => setConfirmAction({ version: v.version, alias: a, isDelete: false })}
                              className="w-full text-left px-3 py-1.5 text-xs hover:bg-accent/30"
                            >
                              <AliasBadge alias={a} />
                            </button>
                          ))}
                        </div>
                      </div>
                      {!v.aliases.includes('Archived') && (
                        <button
                          onClick={() => setConfirmAction({ version: v.version, alias: 'Archived', isDelete: false })}
                          className="px-2 py-1 rounded text-xs font-medium opacity-60 hover:opacity-100 transition-opacity"
                          style={{ background: 'var(--surface-1)', border: '1px solid var(--border)' }}
                        >
                          Archive
                        </button>
                      )}
                    </div>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function CostHistory({ modelName }: { modelName: string }) {
  const { data: costs = [], isLoading } = useQuery<CostRow[]>({
    queryKey: ['model-costs', modelName],
    queryFn: () => apiFetch(`/api/models/${modelName}/costs`),
    enabled: !!modelName,
  })

  if (isLoading) return null
  if (costs.length === 0) return null  // Don't show if no data

  const totalGpu = costs.reduce((s, r) => s + (r.gpu_hours ?? 0), 0)
  const totalCost = costs.reduce((s, r) => s + (r.cost_usd ?? 0), 0)

  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
      <div className="px-4 py-3 flex items-center justify-between" style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border)' }}>
        <p className="text-sm font-semibold">HPC Cost History</p>
        <div className="flex gap-4 text-xs text-muted-foreground">
          <span>GPU-hours: <strong>{totalGpu.toFixed(2)}</strong></span>
          <span>Total: <strong>${totalCost.toFixed(2)}</strong></span>
        </div>
      </div>
      <table className="w-full text-sm" style={{ background: 'var(--surface-0)' }}>
        <thead style={{ background: 'var(--surface-1)' }}>
          <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground">
            {['Version','Run','Job','GPU-Hours','Cost','Recorded'].map(h => <th key={h} className="px-4 py-2">{h}</th>)}
          </tr>
        </thead>
        <tbody>
          {costs.map((r, i) => (
            <tr key={i} className="border-t" style={{ borderColor: 'var(--border-sm)' }}>
              <td className="px-4 py-2 font-mono text-xs">v{r.version}</td>
              <td className="px-4 py-2 font-mono text-xs text-muted-foreground">{(r.run_id ?? '—').slice(0,8)}</td>
              <td className="px-4 py-2 font-mono text-xs text-muted-foreground">{r.job_id ?? '—'}</td>
              <td className="px-4 py-2 font-mono text-xs">{r.gpu_hours != null ? r.gpu_hours.toFixed(2) : '—'}</td>
              <td className="px-4 py-2 font-mono text-xs">{r.cost_usd != null ? `$${r.cost_usd.toFixed(2)}` : '—'}</td>
              <td className="px-4 py-2 text-xs text-muted-foreground">{(r.recorded_at ?? '').slice(0,10)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function ModelDetail() {
  const { name } = useParams<{ name: string }>()
  const { data, isLoading, error } = useModelDetail(name ?? '')

  const [activeTab, setActiveTab] = useState<'overview' | 'versions'>('overview')
  useEffect(() => {
    setActiveTab('overview')
  }, [name])
  const [editing, setEditing] = useState(false)
  const [draftMd, setDraftMd] = useState('')
  const updateDesc = useUpdateDescription(name ?? '')
  const revertDesc = useRevertDescription(name ?? '')
  const uploadImg = useUploadImage(name ?? '')
  const deleteImg = useDeleteImage(name ?? '')
  const predict = usePredict(name ?? '')

  const fileInputRef = useRef<HTMLInputElement>(null)
  const [predictStage, setPredictStage] = useState('Production')
  const [predictJson, setPredictJson] = useState('{}')
  useEffect(() => {
    if (data?.technical?.input_schema && predictJson === '{}') {
      const example: Record<string, unknown> = {}
      for (const [key, type] of Object.entries(data.technical.input_schema as Record<string, string>)) {
        if (type === 'float' || type === 'number') example[key] = 0.0
        else if (type === 'int' || type === 'integer') example[key] = 0
        else if (type === 'bool' || type === 'boolean') example[key] = false
        else if (type.startsWith('list') || type.includes('[]')) example[key] = []
        else example[key] = ''
      }
      if (Object.keys(example).length > 0) {
        setPredictJson(JSON.stringify(example, null, 2))
      }
    }
  }, [data])
  const [predictResult, setPredictResult] = useState<unknown>(null)
  const [predictError, setPredictError] = useState<string | null>(null)
  const [showTry, setShowTry] = useState(false)

  const admin = isAdmin()

  const handleEdit = () => {
    setDraftMd(data?.description.body ?? '')
    setEditing(true)
  }
  const handleCancelEdit = () => setEditing(false)
  const handleSave = async () => {
    await updateDesc.mutateAsync(draftMd)
    setEditing(false)
  }
  const handleRevert = async () => {
    await revertDesc.mutateAsync()
  }
  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    await uploadImg.mutateAsync(file)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }
  const handleDeleteImage = async (id: string) => {
    await deleteImg.mutateAsync(id)
  }
  const handlePredict = async () => {
    setPredictError(null)
    setPredictResult(null)
    try {
      const features = JSON.parse(predictJson)
      const result = await predict.mutateAsync({ features, stage: predictStage || undefined })
      setPredictResult(result)
    } catch (e) {
      setPredictError(e instanceof Error ? e.message : String(e))
    }
  }

  if (isLoading) return (
    <div className="p-6 space-y-4 max-w-4xl mx-auto">
      {[...Array(4)].map((_, i) => (
        <div key={i} className="h-20 rounded-xl animate-pulse"
          style={{ background: 'var(--surface-1)', border: '1px solid var(--border-sm)' }} />
      ))}
    </div>
  )

  if (error || !data) return (
    <div className="p-6 max-w-4xl mx-auto">
      <p className="text-sm rounded-lg px-4 py-3"
        style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
        Failed to load model "{name}".
      </p>
    </div>
  )

  const fm = data.frontmatter
  const statusColors = fm.status ? STATUS_COLORS[fm.status] : null
  const rewrittenBody = rewriteImageUrls(data.description.body, data.images)
  const uploadedImages = data.images.filter(i => i.source === 'uploaded' && i.id)

  return (
    <div className="p-6 space-y-5 max-w-4xl mx-auto">
      {/* Back link */}
      <Link to="/models" className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors">
        <ArrowLeft className="w-3.5 h-3.5" /> Models
      </Link>

      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-2">
          <div className="flex items-center gap-2.5 flex-wrap">
            <h1 className="text-2xl font-bold">{fm.display_name ?? data.name}</h1>
            {fm.status && statusColors && (
              <span className="text-[11px] font-medium px-2 py-0.5 rounded-full uppercase tracking-wide"
                style={{ background: statusColors.bg, border: `1px solid ${statusColors.border}`, color: statusColors.text }}>
                {fm.status}
              </span>
            )}
          </div>
          {fm.summary && <p className="text-sm text-muted-foreground max-w-2xl">{fm.summary}</p>}
          {fm.tags && fm.tags.length > 0 && (
            <div className="flex items-center gap-1.5 flex-wrap">
              <Tag className="w-3 h-3 text-muted-foreground/60" />
              {fm.tags.map(t => (
                <span key={t} className="text-[11px] px-2 py-0.5 rounded-md"
                  style={{ background: 'var(--surface-2)', border: '1px solid var(--border-md)', color: 'var(--subtle-text)' }}>
                  {t}
                </span>
              ))}
            </div>
          )}
          {fm.paper?.url && (
            <a href={fm.paper.url} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-xs transition-colors hover:opacity-80"
              style={{ color: 'var(--accent-text)' }}>
              <ExternalLink className="w-3 h-3" />
              {fm.paper.title ?? 'Paper'}
            </a>
          )}
        </div>
        <div className="flex flex-col items-end gap-1.5 shrink-0">
          <span className="text-xs font-mono px-2 py-1 rounded-md"
            style={{ background: 'var(--surface-2)', border: '1px solid var(--border-md)', color: 'var(--faint-text)' }}>
            {data.task_type}
          </span>
          {data.technical.supported_datasets.length > 0 && (
            <div className="flex flex-wrap justify-end gap-1">
              {data.technical.supported_datasets.map(d => (
                <span key={d} className="text-[11px] px-1.5 py-0.5 rounded-md font-mono"
                  style={{ background: 'var(--surface-2)', border: '1px solid var(--border-sm)', color: 'var(--subtle-text)' }}>
                  {d}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Stages bar */}
      <div className="rounded-xl p-4 flex items-center justify-around gap-4"
        style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}>
        <StageBadge label="Production" stage={data.stages.production} />
        <div className="h-8 w-px" style={{ background: 'var(--border-sm)' }} />
        <StageBadge label="Canary" stage={data.stages.canary} />
        <div className="h-8 w-px" style={{ background: 'var(--border-sm)' }} />
        <StageBadge label="Staging" stage={data.stages.staging} />
      </div>

      {/* Tab bar */}
      <div className="flex gap-1 border-b" style={{ borderColor: 'var(--border)' }}>
        {(['overview', 'versions'] as const).map(tab => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className="px-4 py-2 text-sm font-medium capitalize transition-colors"
            style={activeTab === tab
              ? { borderBottom: '2px solid oklch(0.64 0.20 265)', color: 'oklch(0.64 0.20 265)' }
              : { color: 'var(--muted-foreground)' }}
          >
            {tab}
          </button>
        ))}
      </div>

      {activeTab === 'versions' && <VersionsTab modelName={name ?? ''} admin={admin} />}

      {activeTab === 'overview' && <>
      {/* Description */}
      <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
        <div className="flex items-center justify-between gap-2 px-4 py-3"
          style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border-sm)' }}>
          <span className="text-sm font-semibold">Description</span>
          <div className="flex items-center gap-2">
            {admin && !editing && (
              <>
                <button onClick={handleEdit}
                  className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium transition-colors"
                  style={{ background: 'oklch(0.64 0.20 265 / 12%)', border: '1px solid oklch(0.64 0.20 265 / 25%)', color: 'var(--accent-text)' }}>
                  <Edit3 className="w-3 h-3" /> Edit
                </button>
                {data.description.source === 'override' && (
                  <button onClick={handleRevert} disabled={revertDesc.isPending}
                    className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium transition-colors"
                    style={{ background: 'var(--surface-2)', border: '1px solid var(--border-md)', color: 'var(--subtle-text)' }}>
                    <RotateCcw className="w-3 h-3" /> Revert
                  </button>
                )}
              </>
            )}
            {admin && editing && (
              <>
                <button onClick={handleSave} disabled={updateDesc.isPending}
                  className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium transition-colors"
                  style={{ background: 'oklch(0.64 0.20 265)', border: '1px solid oklch(0.64 0.20 265 / 60%)', color: 'oklch(0.99 0 0)' }}>
                  {updateDesc.isPending ? 'Saving…' : 'Save'}
                </button>
                <button onClick={handleCancelEdit}
                  className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium"
                  style={{ background: 'var(--surface-2)', border: '1px solid var(--border-md)', color: 'var(--subtle-text)' }}>
                  <X className="w-3 h-3" /> Cancel
                </button>
              </>
            )}
          </div>
        </div>

        {data.description.upstream_drift && admin && (
          <Alert style={{ borderRadius: 0, background: 'oklch(0.78 0.18 55 / 10%)', border: 'none', borderBottom: '1px solid oklch(0.78 0.18 55 / 25%)' }}>
            <AlertTriangle className="h-4 w-4" style={{ color: 'var(--warning-text)' }} />
            <AlertDescription style={{ color: 'var(--warning-text)' }}>
              The filesystem README has changed since this override was saved. Review and save to sync, or revert to use the latest filesystem version.
            </AlertDescription>
          </Alert>
        )}

        <div className="p-5" style={{ background: 'var(--surface-0)' }}>
          {editing ? (
            <div data-color-mode="dark">
              <MDEditor
                value={draftMd}
                onChange={v => setDraftMd(v ?? '')}
                height={400}
                preview="live"
              />
            </div>
          ) : data.description.body ? (
            <div className="prose prose-invert prose-sm max-w-none">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{rewrittenBody}</ReactMarkdown>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground italic">No description available.</p>
          )}
        </div>

        {/* Image management (admin only) */}
        {admin && (
          <div className="px-5 pb-5" style={{ background: 'var(--surface-0)' }}>
            <div className="flex items-center gap-2 mb-3">
              <span className="text-xs font-medium text-muted-foreground">Uploaded images</span>
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={uploadImg.isPending}
                className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium transition-colors"
                style={{ background: 'oklch(0.64 0.20 265 / 12%)', border: '1px solid oklch(0.64 0.20 265 / 25%)', color: 'var(--accent-text)' }}>
                <Upload className="w-3 h-3" />
                {uploadImg.isPending ? 'Uploading…' : 'Upload image'}
              </button>
              <input ref={fileInputRef} type="file" accept="image/*" onChange={handleFileChange} className="hidden" />
            </div>
            {uploadedImages.length === 0 ? (
              <p className="text-xs text-muted-foreground/60 italic">No uploaded images. Use <code>dashboard://image/{'<id>'}</code> in markdown to embed them.</p>
            ) : (
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                {uploadedImages.map(img => (
                  <div key={img.id} className="relative group rounded-lg overflow-hidden"
                    style={{ border: '1px solid var(--border-md)' }}>
                    <img src={img.url} alt={img.original_name ?? ''} className="w-full h-20 object-cover" />
                    <div className="absolute inset-0 bg-black/60 opacity-0 group-hover:opacity-100 transition-opacity flex flex-col items-center justify-center gap-1 p-1">
                      <span className="text-[10px] text-white/80 font-mono text-center break-all px-1">{img.placeholder}</span>
                      <button onClick={() => img.id && handleDeleteImage(img.id)}
                        className="p-1 rounded" style={{ background: 'oklch(0.66 0.22 25 / 80%)' }}>
                        <Trash2 className="w-3 h-3" style={{ color: 'oklch(0.99 0 0)' }} />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Technical details */}
      <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
        <div className="px-4 py-3 flex items-center gap-2"
          style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border-sm)' }}>
          <Cpu className="w-4 h-4" style={{ color: 'var(--accent-text)' }} />
          <span className="text-sm font-semibold">Technical</span>
        </div>
        <div className="p-4 grid grid-cols-1 sm:grid-cols-2 gap-4 text-sm"
          style={{ background: 'var(--surface-0)' }}>
          <div>
            <p className="text-xs text-muted-foreground mb-1">Estimator</p>
            <p className="font-mono text-xs">{data.technical.estimator_class}</p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground mb-1">Input schema</p>
            <pre className="text-xs font-mono p-2 rounded-lg overflow-auto"
              style={{ background: 'var(--surface-deep)', border: '1px solid var(--border-sm)' }}>
              {JSON.stringify(data.technical.input_schema, null, 2)}
            </pre>
          </div>
          <div>
            <p className="text-xs text-muted-foreground mb-1">Output schema</p>
            <pre className="text-xs font-mono p-2 rounded-lg overflow-auto"
              style={{ background: 'var(--surface-deep)', border: '1px solid var(--border-sm)' }}>
              {JSON.stringify(data.technical.output_schema, null, 2)}
            </pre>
          </div>
          {data.technical.promotion.metric && (
            <div>
              <p className="text-xs text-muted-foreground mb-1">Promotion gate (Production)</p>
              <p className="text-xs font-mono">
                {data.technical.promotion.metric} {data.technical.promotion.direction === 'lower_is_better' ? '≤' : '≥'} {data.technical.promotion.threshold}
              </p>
            </div>
          )}
          {data.technical.hyperparameters && Object.keys(data.technical.hyperparameters).length > 0 && (
            <div>
              <p className="text-xs text-muted-foreground mb-1">Hyperparameters</p>
              <pre className="text-xs font-mono p-2 rounded-lg overflow-auto"
                style={{ background: 'var(--surface-deep)', border: '1px solid var(--border-sm)' }}>
                {JSON.stringify(data.technical.hyperparameters, null, 2)}
              </pre>
            </div>
          )}
        </div>
      </div>

      {/* Lifecycle Gates */}
      {data.lifecycle_gates && data.lifecycle_gates.length > 0 && (
        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
          <div className="px-4 py-3 flex items-center gap-2"
            style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border-sm)' }}>
            <Layers className="w-4 h-4" style={{ color: 'var(--accent-text)' }} />
            <span className="text-sm font-semibold">Lifecycle Gates</span>
            <span className="ml-auto text-xs text-muted-foreground">promotion thresholds per stage</span>
          </div>
          <div className="p-4 flex flex-col sm:flex-row gap-3" style={{ background: 'var(--surface-0)' }}>
            {data.lifecycle_gates.map((gate: { name: string; metric: string; threshold: number; direction: string }, idx: number) => {
              const stageColors: Record<string, { bg: string; border: string; text: string }> = {
                Staging:    { bg: 'oklch(0.64 0.20 265 / 10%)', border: 'oklch(0.64 0.20 265 / 28%)', text: 'oklch(0.50 0.20 265)' },
                Canary:     { bg: 'oklch(0.80 0.18 80 / 10%)',  border: 'oklch(0.80 0.18 80 / 28%)',  text: 'oklch(0.55 0.18 80)'  },
                Production: { bg: 'oklch(0.72 0.18 155 / 10%)', border: 'oklch(0.72 0.18 155 / 28%)', text: 'oklch(0.48 0.18 155)' },
              }
              const c = stageColors[gate.name] ?? { bg: 'var(--surface-2)', border: 'var(--border-md)', text: 'var(--subtle-text)' }
              const op = gate.direction === 'lower_is_better' ? '≤' : '≥'
              const stageVersion = (data.stages as Record<string, { version: string } | null>)[gate.name.toLowerCase()]
              return (
                <div key={idx} className="flex-1 rounded-lg p-3 space-y-1.5"
                  style={{ background: c.bg, border: `1px solid ${c.border}` }}>
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold" style={{ color: c.text }}>{gate.name}</span>
                    {stageVersion && (
                      <span className="text-[10px] font-mono px-1.5 py-0.5 rounded"
                        style={{ background: c.border, color: c.text }}>
                        v{stageVersion.version}
                      </span>
                    )}
                  </div>
                  <p className="text-xs font-mono" style={{ color: 'var(--foreground)' }}>
                    {gate.metric} {op} <strong>{gate.threshold}</strong>
                  </p>
                  <p className="text-[10px]" style={{ color: 'var(--muted-foreground)' }}>
                    {gate.direction === 'lower_is_better' ? 'lower is better' : 'higher is better'}
                  </p>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Retraining Schedule */}
      {data.retraining?.schedule && (
        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
          <div className="px-4 py-3 flex items-center gap-2"
            style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border-sm)' }}>
            <Clock className="w-4 h-4" style={{ color: 'var(--accent-text)' }} />
            <span className="text-sm font-semibold">Retraining Schedule</span>
          </div>
          <div className="p-4 grid grid-cols-1 sm:grid-cols-2 gap-4 text-sm" style={{ background: 'var(--surface-0)' }}>
            <div>
              <p className="text-xs text-muted-foreground mb-1">Cron schedule</p>
              <code className="text-xs font-mono px-2 py-1 rounded"
                style={{ background: 'var(--surface-deep)', border: '1px solid var(--border-sm)', color: 'var(--accent-text)' }}>
                {data.retraining.schedule}
              </code>
            </div>
            {data.retraining.deployment_name && (
              <div>
                <p className="text-xs text-muted-foreground mb-1">Prefect deployment</p>
                <p className="text-xs font-mono">{data.retraining.deployment_name}</p>
              </div>
            )}
            {data.retraining.work_pool && (
              <div>
                <p className="text-xs text-muted-foreground mb-1">Work pool</p>
                <p className="text-xs font-mono">{data.retraining.work_pool}</p>
              </div>
            )}
            {data.retraining.concurrency_limit != null && (
              <div>
                <p className="text-xs text-muted-foreground mb-1">Concurrency limit</p>
                <p className="text-xs font-mono">{data.retraining.concurrency_limit}</p>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Links */}
      {Object.keys(data.links).length > 0 && (
        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
          <div className="px-4 py-3 flex items-center gap-2"
            style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border-sm)' }}>
            <ExternalLink className="w-4 h-4" style={{ color: 'var(--accent-text)' }} />
            <span className="text-sm font-semibold">Links</span>
          </div>
          <div className="p-4 flex flex-wrap gap-2" style={{ background: 'var(--surface-0)' }}>
            {Object.entries(data.links).map(([label, url]) => (
              <LinkButton key={label} label={label} url={url} />
            ))}
          </div>
        </div>
      )}

      {/* Try it out */}
      <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
        <button
          onClick={() => setShowTry(o => !o)}
          className="w-full flex items-center gap-2 px-4 py-3 text-left transition-colors hover:bg-accent/30"
          style={{ background: 'var(--surface-1)', borderBottom: showTry ? '1px solid var(--border-sm)' : 'none' }}>
          <BarChart3 className="w-4 h-4" style={{ color: 'var(--accent-text)' }} />
          <span className="text-sm font-semibold flex-1">Try it out</span>
          <ChevronDown className={`w-4 h-4 text-muted-foreground transition-transform ${showTry ? 'rotate-180' : ''}`} />
        </button>
        {showTry && (
          <div className="p-4 space-y-3" style={{ background: 'var(--surface-0)' }}>
            <div className="flex items-center gap-3 flex-wrap">
              <div className="space-y-1">
                <label className="text-xs text-muted-foreground">Stage</label>
                <select
                  value={predictStage}
                  onChange={e => setPredictStage(e.target.value)}
                  className="rounded-lg px-3 py-1.5 text-sm focus:outline-none"
                  style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }}>
                  <option value="Production">Production</option>
                  <option value="Canary">Canary</option>
                  <option value="Staging">Staging</option>
                </select>
              </div>
            </div>
            <div className="space-y-1">
              <label className="text-xs text-muted-foreground">Features (JSON)</label>
              <textarea
                value={predictJson}
                onChange={e => setPredictJson(e.target.value)}
                rows={4}
                className="w-full rounded-lg px-3 py-2 text-sm font-mono focus:outline-none resize-none"
                style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }}
                placeholder='{"submit_time": 1234.5}'
              />
            </div>
            <button
              onClick={handlePredict}
              disabled={predict.isPending}
              className="inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium disabled:opacity-50"
              style={{ background: 'oklch(0.64 0.20 265)', border: '1px solid oklch(0.64 0.20 265 / 60%)', color: 'oklch(0.99 0 0)' }}>
              <Send className="w-3.5 h-3.5" />
              {predict.isPending ? 'Sending…' : 'Send'}
            </button>
            {predictError && (
              <p className="text-xs rounded-lg px-3 py-2"
                style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
                {predictError}
              </p>
            )}
            {predictResult !== null && (
              <pre className="text-xs font-mono rounded-lg p-3 overflow-auto"
                style={{ background: 'var(--surface-deep)', border: '1px solid var(--border-sm)', color: 'var(--success-text)' }}>
                {JSON.stringify(predictResult, null, 2)}
              </pre>
            )}
          </div>
        )}
      </div>

      <CostHistory modelName={name ?? ''} />
      </>}
    </div>
  )
}
