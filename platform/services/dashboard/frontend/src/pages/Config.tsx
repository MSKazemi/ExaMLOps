import { useEffect, useRef, useState } from 'react'
import { CliConfigSection } from '@/components/cli/CliConfigSection'
import { CliFlagGate } from '@/components/cli/CliFlagGate'
import {
  Link2, SlidersHorizontal, Server, Save, ChevronDown, ChevronRight,
  KeyRound, Eye, EyeOff, X, Copy, Check, ExternalLink, RefreshCw, Zap, Clock, Lock, GitBranch,
  Download, Upload,
} from 'lucide-react'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { useQueryClient } from '@tanstack/react-query'
import {
  apiFetch, useConfig, useConfigKeys, useMe, useUpdateConfig, useHealth, useReloadRay, exportEnv, importEnv,
} from '@/lib/api'
import type { ConfigKeyMeta } from '@/lib/api'
import { getToken, isAdmin } from '@/lib/auth'

// Maps a config key to the service name returned by /api/health
const HEALTH_KEY_MAP: Record<string, string> = {
  mlflow_url:        'mlflow',
  prefect_url:       'prefect',
  ray_serve_url:     'ray_serve',
  ray_dashboard_url: 'ray_serve',
  prometheus_url:    'prometheus',
  grafana_url:       'grafana',
  minio_url:         'minio',
  minio_console_url: 'minio',
}

const ENDPOINT_KEYS = [
  { key: 'mlflow_url',        label: 'MLflow URL' },
  { key: 'prefect_url',       label: 'Prefect URL' },
  { key: 'ray_serve_url',     label: 'Ray Serve URL' },
  { key: 'ray_dashboard_url', label: 'Ray Dashboard URL' },
  { key: 'prometheus_url',    label: 'Prometheus URL' },
  { key: 'grafana_url',       label: 'Grafana URL' },
  { key: 'minio_url',         label: 'MinIO S3 API URL' },
  { key: 'minio_console_url', label: 'MinIO Console URL' },
]

const THRESHOLD_KEYS = [
  { key: 'threshold_jpcp_rmse', label: 'JPCP — RMSE threshold' },
]

const SLURM_KEYS = [
  { key: 'slurm_mode',          label: 'Mode (mock | slurm)' },
  { key: 'slurm_partition',     label: 'Partition' },
  { key: 'slurm_cpus_per_task', label: 'CPUs per task' },
  { key: 'slurm_mem',           label: 'Memory (e.g. 16G)' },
  { key: 'slurm_time',          label: 'Wall-clock time (e.g. 2:00:00)' },
]

const CREDENTIAL_KEYS = [
  { key: 'minio_access_key',        label: 'MinIO Access Key' },
  { key: 'minio_secret_key',        label: 'MinIO Secret Key' },
  { key: 'grafana_api_key',         label: 'Grafana API Key' },
  { key: 'control_plane_token',     label: 'Control Plane Token' },
  { key: 'gitlab_pipeline_token',   label: 'GitLab Pipeline Trigger Token' },
]

type SecretState = { typed: string; cleared: boolean }

// ── Shared section wrapper ───────────────────────────────────────────────────

function SectionHeader({
  icon: Icon, title, note, open, onToggle, badge,
}: {
  icon: React.ComponentType<{ className?: string; style?: React.CSSProperties }>
  title: string
  note?: string
  open: boolean
  onToggle: () => void
  badge?: React.ReactNode
}) {
  return (
    <button
      onClick={onToggle}
      className="w-full flex items-center gap-3 p-4 text-left transition-colors hover:bg-accent/30"
      style={{ background: 'var(--surface-1)' }}
    >
      <div
        className="w-7 h-7 rounded-md flex items-center justify-center shrink-0"
        style={{ background: 'oklch(0.64 0.20 265 / 15%)', border: '1px solid oklch(0.64 0.20 265 / 25%)' }}
      >
        <Icon className="w-3.5 h-3.5" style={{ color: 'var(--accent-text)' }} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <p className="font-semibold text-sm">{title}</p>
          {badge}
        </div>
        {note && <p className="text-[11px] text-muted-foreground mt-0.5 truncate">{note}</p>}
      </div>
      {open
        ? <ChevronDown className="w-4 h-4 text-muted-foreground shrink-0" />
        : <ChevronRight className="w-4 h-4 text-muted-foreground shrink-0" />}
    </button>
  )
}

// ── Service Endpoints ────────────────────────────────────────────────────────

function EndpointField({
  fieldKey, label, value, onChange, readOnly, status,
}: {
  fieldKey: string
  label: string
  value: string
  onChange: (v: string) => void
  readOnly: boolean
  status?: string | null
}) {
  const [copied, setCopied] = useState(false)

  const copy = () => {
    if (!value) return
    navigator.clipboard.writeText(value).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  const statusColor =
    status === 'ok'       ? '#10b981' :
    status === 'degraded' ? '#f59e0b' :
    status === 'down'     ? '#ef4444' : null

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1.5">
        {statusColor && (
          <span
            className="w-1.5 h-1.5 rounded-full shrink-0 inline-block"
            style={{ background: statusColor }}
            title={status ?? undefined}
          />
        )}
        <label htmlFor={fieldKey} className="text-xs font-medium text-muted-foreground">
          {label}
        </label>
      </div>
      <div className="flex items-center gap-1">
        <input
          id={fieldKey}
          value={value}
          onChange={e => onChange(e.target.value)}
          placeholder="http://…"
          readOnly={readOnly}
          className="flex-1 min-w-0 rounded-lg px-3 py-2 text-sm placeholder:text-muted-foreground/50 focus:outline-none"
          style={{
            background: 'var(--input-bg)',
            border: '1px solid var(--border-md)',
            color: 'var(--foreground)',
          }}
        />
        {value && (
          <>
            <button
              type="button"
              onClick={copy}
              title="Copy URL"
              className="p-1.5 rounded-md text-muted-foreground hover:text-foreground transition-colors shrink-0"
            >
              {copied
                ? <Check className="w-3.5 h-3.5" style={{ color: 'var(--success-text)' }} />
                : <Copy className="w-3.5 h-3.5" />}
            </button>
            <a
              href={value}
              target="_blank"
              rel="noopener noreferrer"
              title="Open in new tab"
              className="p-1.5 rounded-md text-muted-foreground hover:text-foreground transition-colors shrink-0"
            >
              <ExternalLink className="w-3.5 h-3.5" />
            </a>
          </>
        )}
      </div>
    </div>
  )
}

function EndpointsSection({
  values, onChange, readOnly, services,
}: {
  values: Record<string, string>
  onChange: (k: string, v: string) => void
  readOnly: boolean
  services: Record<string, { status: string; url: string }> | null
}) {
  const [open, setOpen] = useState(false)

  const liveCount = services
    ? ENDPOINT_KEYS.filter(({ key }) => {
        const svc = HEALTH_KEY_MAP[key]
        return svc && services[svc]?.status === 'ok'
      }).length
    : null

  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
      <SectionHeader
        icon={Link2}
        title="Service Endpoints"
        open={open}
        onToggle={() => setOpen(o => !o)}
        badge={liveCount !== null ? (
          <span
            className="text-[10px] font-medium px-1.5 py-0.5 rounded-full"
            style={{
              background: 'oklch(0.72 0.18 155 / 15%)',
              border: '1px solid oklch(0.72 0.18 155 / 25%)',
              color: 'var(--success-text)',
            }}
          >
            {liveCount} live
          </span>
        ) : undefined}
      />
      {open && (
        <div className="p-4 grid grid-cols-1 sm:grid-cols-2 gap-3" style={{ background: 'var(--surface-0)' }}>
          {ENDPOINT_KEYS.map(({ key, label }) => {
            const svcName = HEALTH_KEY_MAP[key]
            const svcStatus = svcName ? services?.[svcName]?.status : null
            return (
              <EndpointField
                key={key}
                fieldKey={key}
                label={label}
                value={values[key] ?? ''}
                onChange={v => onChange(key, v)}
                readOnly={readOnly}
                status={svcStatus}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── Credentials ──────────────────────────────────────────────────────────────

function CredentialsSection({
  state, hasValueByKey, keysMeta, onType, onClear, readOnly,
}: {
  state: Record<string, SecretState>
  hasValueByKey: Record<string, boolean>
  keysMeta: ConfigKeyMeta[] | undefined
  onType: (k: string, v: string) => void
  onClear: (k: string) => void
  readOnly: boolean
}) {
  const [open, setOpen] = useState(false)
  const [show, setShow] = useState<Record<string, boolean>>({})

  const getUpdatedAt = (key: string) => {
    const meta = keysMeta?.find(m => m.key === key)
    if (!meta?.has_value) return null
    return new Date(meta.updated_at).toLocaleDateString(undefined, {
      month: 'short', day: 'numeric', year: 'numeric',
    })
  }

  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
      <SectionHeader
        icon={KeyRound}
        title="Credentials"
        note="Encrypted at rest — blank input leaves value unchanged; × clears"
        open={open}
        onToggle={() => setOpen(o => !o)}
      />
      {open && (
        <div className="p-4 grid grid-cols-1 sm:grid-cols-2 gap-4" style={{ background: 'var(--surface-0)' }}>
          {CREDENTIAL_KEYS.map(({ key, label }) => {
            const has = hasValueByKey[key] === true
            const cleared = state[key]?.cleared === true
            const typed = state[key]?.typed ?? ''
            const visible = show[key] === true
            const updatedAt = getUpdatedAt(key)

            // Placeholder reflects actual state clearly
            const placeholder = cleared
              ? 'Will be cleared on save'
              : has
              ? 'Stored — type new value to replace'
              : 'Not set'

            return (
              <div key={key} className="space-y-1.5">
                <div className="flex items-center justify-between gap-2">
                  <label htmlFor={key} className="text-xs font-medium text-muted-foreground">
                    {label}
                  </label>
                  <div className="flex items-center gap-1.5 shrink-0">
                    {updatedAt && (
                      <span className="text-[10px] text-muted-foreground/70 flex items-center gap-0.5">
                        <Clock className="w-2.5 h-2.5" />
                        {updatedAt}
                      </span>
                    )}
                    {has && !cleared && !updatedAt && (
                      <span
                        className="text-[10px] px-1.5 py-0.5 rounded-full"
                        style={{ background: 'oklch(0.72 0.18 155 / 12%)', color: 'var(--success-text)' }}
                      >
                        set
                      </span>
                    )}
                    {cleared && (
                      <span
                        className="text-[10px] px-1.5 py-0.5 rounded-full"
                        style={{ background: 'oklch(0.66 0.22 25 / 12%)', color: 'var(--error-text)' }}
                      >
                        clearing
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  <input
                    id={key}
                    type={typed && visible ? 'text' : 'password'}
                    value={typed}
                    onChange={e => onType(key, e.target.value)}
                    placeholder={placeholder}
                    readOnly={readOnly}
                    className="flex-1 min-w-0 rounded-lg px-3 py-2 text-sm placeholder:text-muted-foreground/50 focus:outline-none"
                    style={{
                      background: 'var(--input-bg)',
                      border: `1px solid ${cleared ? 'oklch(0.66 0.22 25 / 40%)' : 'var(--border-md)'}`,
                      color: 'var(--foreground)',
                    }}
                  />
                  {/* Eye button: only relevant when user has typed something */}
                  {typed ? (
                    <button
                      type="button"
                      onClick={() => setShow(s => ({ ...s, [key]: !s[key] }))}
                      title={visible ? `Hide ${label}` : `Reveal ${label}`}
                      className="p-1.5 rounded-md text-muted-foreground hover:text-foreground transition-colors shrink-0"
                    >
                      {visible ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                    </button>
                  ) : has ? (
                    <span
                      className="p-1.5 shrink-0 text-muted-foreground/40"
                      title="Stored encrypted — type to replace"
                    >
                      <Lock className="w-3.5 h-3.5" />
                    </span>
                  ) : null}
                  {has && !readOnly && (
                    <button
                      type="button"
                      onClick={() => onClear(key)}
                      title={`Clear ${label}`}
                      className="p-1.5 rounded-md text-muted-foreground hover:text-foreground transition-colors shrink-0"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── Quick Actions ────────────────────────────────────────────────────────────

function QuickActionsSection() {
  const reloadRay = useReloadRay()

  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
      <div
        className="flex items-center gap-3 p-4"
        style={{ background: 'var(--surface-1)' }}
      >
        <div
          className="w-7 h-7 rounded-md flex items-center justify-center shrink-0"
          style={{ background: 'oklch(0.64 0.20 265 / 15%)', border: '1px solid oklch(0.64 0.20 265 / 25%)' }}
        >
          <Zap className="w-3.5 h-3.5" style={{ color: 'var(--accent-text)' }} />
        </div>
        <div className="flex-1 min-w-0">
          <p className="font-semibold text-sm">Quick Actions</p>
          <p className="text-[11px] text-muted-foreground mt-0.5">
            Runtime operations — no config save required
          </p>
        </div>
      </div>
      <div
        className="p-4 flex flex-wrap items-center gap-3"
        style={{ background: 'var(--surface-0)' }}
      >
        <button
          type="button"
          onClick={() => reloadRay.mutate()}
          disabled={reloadRay.isPending}
          className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-all disabled:opacity-50 hover:bg-accent/40"
          style={{
            background: 'var(--surface-2)',
            border: '1px solid var(--border-md)',
            color: 'var(--foreground)',
          }}
        >
          <RefreshCw className={`w-3.5 h-3.5 ${reloadRay.isPending ? 'animate-spin' : ''}`} />
          {reloadRay.isPending ? 'Reloading…' : 'Reload Ray Serve'}
        </button>
        {reloadRay.isSuccess && (
          <span className="text-xs flex items-center gap-1" style={{ color: 'var(--success-text)' }}>
            <Check className="w-3.5 h-3.5" />
            {reloadRay.data?.count ?? 0} model(s) reloaded
          </span>
        )}
        {reloadRay.isError && (
          <span className="text-xs" style={{ color: 'var(--error-text)' }}>
            Reload failed
          </span>
        )}
      </div>
    </div>
  )
}

// ── Generic plain-value section ───────────────────────────────────────────────

function PlainSection({
  title, icon: Icon, note, keys, values, onChange, readOnly, badge,
}: {
  title: string
  icon: React.ComponentType<{ className?: string; style?: React.CSSProperties }>
  note?: string
  keys: { key: string; label: string }[]
  values: Record<string, string>
  onChange: (k: string, v: string) => void
  readOnly: boolean
  badge?: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
      <SectionHeader
        icon={Icon}
        title={title}
        note={note}
        open={open}
        onToggle={() => setOpen(o => !o)}
        badge={badge}
      />
      {open && (
        <div className="p-4 grid grid-cols-1 sm:grid-cols-2 gap-3" style={{ background: 'var(--surface-0)' }}>
          {keys.map(({ key, label }) => (
            <div key={key} className="space-y-1.5">
              <label htmlFor={key} className="text-xs font-medium text-muted-foreground">
                {label}
              </label>
              <input
                id={key}
                value={values[key] ?? ''}
                onChange={e => onChange(key, e.target.value)}
                placeholder={`Enter ${label.toLowerCase()}`}
                readOnly={readOnly}
                className="w-full rounded-lg px-3 py-2 text-sm placeholder:text-muted-foreground/50 focus:outline-none"
                style={{
                  background: 'var(--input-bg)',
                  border: '1px solid var(--border-md)',
                  color: 'var(--foreground)',
                }}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── GitLab ModelZoo ───────────────────────────────────────────────────────────

function GitLabSection({
  gitlabUrl,
  onGitlabUrlChange,
  projectId,
  onProjectIdChange,
  secretState,
  hasToken,
  tokenMeta,
  onTokenType,
  onTokenClear,
  readOnly,
}: {
  gitlabUrl: string
  onGitlabUrlChange: (v: string) => void
  projectId: string
  onProjectIdChange: (v: string) => void
  secretState: SecretState | undefined
  hasToken: boolean
  tokenMeta: ConfigKeyMeta | undefined
  onTokenType: (v: string) => void
  onTokenClear: () => void
  readOnly: boolean
}) {
  const [open, setOpen] = useState(false)
  const [showToken, setShowToken] = useState(false)

  const isConfigured = !!projectId && hasToken
  const badge = isConfigured ? (
    <span
      className="text-[10px] font-medium px-1.5 py-0.5 rounded-full"
      style={{
        background: 'oklch(0.72 0.18 155 / 15%)',
        border: '1px solid oklch(0.72 0.18 155 / 25%)',
        color: 'var(--success-text)',
      }}
    >
      configured
    </span>
  ) : (
    <span
      className="text-[10px] font-medium px-1.5 py-0.5 rounded-full"
      style={{
        background: 'oklch(0.78 0.18 80 / 12%)',
        border: '1px solid oklch(0.78 0.18 80 / 25%)',
        color: 'var(--warning-text)',
      }}
    >
      not set
    </span>
  )

  const typed = secretState?.typed ?? ''
  const cleared = secretState?.cleared ?? false

  const tokenUpdatedAt = tokenMeta?.has_value
    ? new Date(tokenMeta.updated_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
    : null

  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
      <SectionHeader
        icon={GitBranch}
        title="GitLab ModelZoo"
        note="Enables live model and dataset auto-discovery from the GitLab repository"
        open={open}
        onToggle={() => setOpen(o => !o)}
        badge={badge}
      />
      {open && (
        <div className="p-4 space-y-4" style={{ background: 'var(--surface-0)' }}>
          {/* GitLab URL */}
          <div className="space-y-1.5">
            <label htmlFor="gitlab_url" className="text-xs font-medium text-muted-foreground">
              GitLab instance URL
            </label>
            <input
              id="gitlab_url"
              value={gitlabUrl}
              onChange={e => onGitlabUrlChange(e.target.value)}
              placeholder="https://gitlab.com"
              readOnly={readOnly}
              className="w-full rounded-lg px-3 py-2 text-sm placeholder:text-muted-foreground/50 focus:outline-none"
              style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }}
            />
            <p className="text-[10px] text-muted-foreground/60">
              Self-hosted? Use your instance URL, e.g. <code className="px-1 rounded" style={{ background: 'var(--surface-2)' }}>https://gitlab.example.com</code>
            </p>
          </div>

          {/* Project ID */}
          <div className="space-y-1.5">
            <label htmlFor="gitlab_project_id" className="text-xs font-medium text-muted-foreground">
              Project ID or path
            </label>
            <input
              id="gitlab_project_id"
              value={projectId}
              onChange={e => onProjectIdChange(e.target.value)}
              placeholder="e.g. my-group/seanergys-modelzoo  or  12345678"
              readOnly={readOnly}
              className="w-full rounded-lg px-3 py-2 text-sm placeholder:text-muted-foreground/50 focus:outline-none"
              style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }}
            />
            <p className="text-[10px] text-muted-foreground/60">
              Find it in GitLab → project → Settings → General (Project ID) or use the namespace/project path.
            </p>
          </div>

          {/* Access Token */}
          <div className="space-y-1.5">
            <div className="flex items-center justify-between gap-2">
              <label htmlFor="gitlab_token" className="text-xs font-medium text-muted-foreground">
                Personal / Project Access Token
              </label>
              <div className="flex items-center gap-1.5 shrink-0">
                {tokenUpdatedAt && (
                  <span className="text-[10px] text-muted-foreground/70 flex items-center gap-0.5">
                    <Clock className="w-2.5 h-2.5" /> {tokenUpdatedAt}
                  </span>
                )}
                {hasToken && !cleared && !tokenUpdatedAt && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded-full"
                    style={{ background: 'oklch(0.72 0.18 155 / 12%)', color: 'var(--success-text)' }}>
                    set
                  </span>
                )}
                {cleared && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded-full"
                    style={{ background: 'oklch(0.66 0.22 25 / 12%)', color: 'var(--error-text)' }}>
                    clearing
                  </span>
                )}
              </div>
            </div>
            <div className="flex items-center gap-1">
              <input
                id="gitlab_token"
                type={typed && showToken ? 'text' : 'password'}
                value={typed}
                onChange={e => onTokenType(e.target.value)}
                placeholder={cleared ? 'Will be cleared on save' : hasToken ? 'Stored — type to replace' : 'glpat-…'}
                readOnly={readOnly}
                className="flex-1 min-w-0 rounded-lg px-3 py-2 text-sm font-mono placeholder:text-muted-foreground/50 focus:outline-none"
                style={{
                  background: 'var(--input-bg)',
                  border: `1px solid ${cleared ? 'oklch(0.66 0.22 25 / 40%)' : 'var(--border-md)'}`,
                  color: 'var(--foreground)',
                }}
              />
              {typed ? (
                <button type="button" onClick={() => setShowToken(s => !s)}
                  className="p-1.5 rounded-md text-muted-foreground hover:text-foreground transition-colors shrink-0">
                  {showToken ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                </button>
              ) : hasToken ? (
                <span className="p-1.5 shrink-0 text-muted-foreground/40">
                  <Lock className="w-3.5 h-3.5" />
                </span>
              ) : null}
              {hasToken && !readOnly && (
                <button type="button" onClick={onTokenClear}
                  className="p-1.5 rounded-md text-muted-foreground hover:text-foreground transition-colors shrink-0">
                  <X className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
            <p className="text-[10px] text-muted-foreground/60">
              Needs <code className="px-1 rounded" style={{ background: 'var(--surface-2)' }}>read_repository</code> scope.
              Create one at GitLab → User Settings → Access Tokens.
            </p>
          </div>
        </div>
      )}
    </div>
  )
}

// ── ModelZoo Webhook ──────────────────────────────────────────────────────────

function ModelzooWebhookSection({
  autoRetrain,
  onAutoRetrainChange,
  webhookUrl,
  onSave,
  saving,
  readOnly,
}: {
  autoRetrain: boolean
  onAutoRetrainChange: (v: boolean) => void
  webhookUrl: string
  onSave: () => void
  saving: boolean
  readOnly: boolean
}) {
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(false)

  const copy = () => {
    navigator.clipboard.writeText(webhookUrl).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  const badge = autoRetrain ? (
    <span
      className="text-[10px] font-medium px-1.5 py-0.5 rounded-full"
      style={{
        background: 'oklch(0.72 0.18 155 / 15%)',
        border: '1px solid oklch(0.72 0.18 155 / 25%)',
        color: 'var(--success-text)',
      }}
    >
      auto-retrain on
    </span>
  ) : (
    <span
      className="text-[10px] font-medium px-1.5 py-0.5 rounded-full"
      style={{
        background: 'var(--surface-2)',
        border: '1px solid var(--border)',
        color: 'var(--muted-foreground)',
      }}
    >
      auto-retrain off
    </span>
  )

  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
      <SectionHeader
        icon={Link2}
        title="ModelZoo Integration"
        note="Webhook and polling configuration for ModelZoo push events"
        open={open}
        onToggle={() => setOpen(o => !o)}
        badge={badge}
      />
      {open && (
        <div className="p-4 space-y-4" style={{ background: 'var(--surface-0)' }}>
          {/* Webhook URL */}
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">
              GitLab webhook URL
            </label>
            <div className="flex items-center gap-1">
              <input
                readOnly
                value={webhookUrl}
                className="flex-1 min-w-0 rounded-lg px-3 py-2 text-xs font-mono focus:outline-none"
                style={{
                  background: 'var(--input-bg)',
                  border: '1px solid var(--border-md)',
                  color: 'var(--foreground)',
                }}
              />
              <button
                type="button"
                onClick={copy}
                title="Copy webhook URL"
                className="shrink-0 rounded-lg p-2 transition-colors"
                style={{ background: 'var(--surface-2)', border: '1px solid var(--border-md)' }}
              >
                {copied
                  ? <Check className="w-3.5 h-3.5" style={{ color: 'var(--success-text)' }} />
                  : <Copy className="w-3.5 h-3.5 text-muted-foreground" />}
              </button>
            </div>
            <p className="text-[10px] text-muted-foreground/60">
              Register this URL as a push webhook in GitLab → Settings → Webhooks.
              Set the secret token to the value of{' '}
              <code className="px-1 rounded" style={{ background: 'var(--surface-2)' }}>MODELZOO_WEBHOOK_SECRET</code>.
            </p>
          </div>

          {/* Auto-retrain toggle */}
          <div className="flex items-center gap-3">
            <input
              type="checkbox"
              id="mz_auto_retrain"
              checked={autoRetrain}
              disabled={readOnly}
              onChange={e => onAutoRetrainChange(e.target.checked)}
              className="w-4 h-4 rounded"
            />
            <label htmlFor="mz_auto_retrain" className="text-sm cursor-pointer">
              Auto-retrain all models when a push to{' '}
              <code className="text-xs px-1 rounded" style={{ background: 'var(--surface-2)' }}>main</code>{' '}
              is detected
            </label>
          </div>

          {/* Save button */}
          {!readOnly && (
            <button
              type="button"
              disabled={saving}
              onClick={onSave}
              className="inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-150 disabled:opacity-50"
              style={{
                background: 'oklch(0.64 0.20 265)',
                color: 'oklch(0.99 0 0)',
                border: '1px solid oklch(0.64 0.20 265 / 60%)',
              }}
            >
              <Save className="w-3.5 h-3.5" />
              {saving ? 'Saving…' : 'Save webhook config'}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

// ── API Token ─────────────────────────────────────────────────────────────────

function ApiTokenSection() {
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const token = getToken()

  const handleCopy = () => {
    if (!token) return
    navigator.clipboard.writeText(token).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
      <SectionHeader
        icon={KeyRound}
        title="API Token"
        note="Current session bearer token for direct API calls"
        open={open}
        onToggle={() => setOpen(o => !o)}
        badge={token ? (
          <span
            className="text-[10px] font-medium px-1.5 py-0.5 rounded-full"
            style={{
              background: 'oklch(0.72 0.18 155 / 12%)',
              border: '1px solid oklch(0.72 0.18 155 / 25%)',
              color: 'var(--success-text)',
            }}
          >
            active
          </span>
        ) : undefined}
      />
      {open && (
        <div className="p-4 space-y-3" style={{ background: 'var(--surface-deep)' }}>
          <p className="text-xs text-muted-foreground">
            Use as{' '}
            <code
              className="text-xs px-1 py-0.5 rounded"
              style={{ background: 'var(--surface-2)' }}
            >
              Authorization: Bearer &lt;token&gt;
            </code>{' '}
            for direct API calls.
          </p>
          <div className="flex items-center gap-2">
            <code
              className="flex-1 min-w-0 text-xs break-all rounded-lg px-3 py-2 font-mono"
              style={{
                background: 'var(--surface-deep)',
                border: '1px solid var(--border-sm)',
                color: 'var(--accent-text)',
              }}
            >
              {token ?? '—'}
            </code>
            <button
              type="button"
              onClick={handleCopy}
              disabled={!token}
              title="Copy token"
              className="shrink-0 p-2 rounded-lg transition-colors hover:bg-accent/30 disabled:opacity-40"
              style={{ border: '1px solid var(--border)' }}
            >
              {copied
                ? <Check className="w-4 h-4" style={{ color: 'var(--success-text)' }} />
                : <Copy className="w-4 h-4 text-muted-foreground" />}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Config page ───────────────────────────────────────────────────────────────

export function Config() {
  const qc = useQueryClient()
  const { data: savedConfig } = useConfig()
  const { data: keysMeta } = useConfigKeys()
  const { data: me } = useMe()
  const { data: health } = useHealth()
  const updateConfig = useUpdateConfig()
  const [plain, setPlain] = useState<Record<string, string>>({})
  const [secrets, setSecrets] = useState<Record<string, SecretState>>({})

  // Sync the editable form state from server config when it (re)loads — the React-docs
  // "adjust state during render when data changes" pattern, equivalent to the old effect.
  const [prevSavedConfig, setPrevSavedConfig] = useState(savedConfig)
  if (savedConfig !== prevSavedConfig) {
    setPrevSavedConfig(savedConfig)
    if (savedConfig) {
      const next: Record<string, string> = {}
      for (const [k, v] of Object.entries(savedConfig)) {
        if (typeof v === 'string' && v !== '***') next[k] = v
      }
      setPlain(next)
    }
  }

  const role = me?.role
  const readOnly = role !== 'admin'

  const hasValueByKey = Object.fromEntries(
    (keysMeta ?? []).map(k => [k.key, k.has_value]),
  )

  const services = health?.services ?? null

  const slurmMode = plain['slurm_mode']
  const slurmBadge = slurmMode ? (
    <span
      className="text-[10px] font-medium px-1.5 py-0.5 rounded-full"
      style={{
        background: slurmMode === 'slurm' ? 'oklch(0.64 0.20 265 / 15%)' : 'var(--surface-2)',
        border: slurmMode === 'slurm' ? '1px solid oklch(0.64 0.20 265 / 25%)' : '1px solid var(--border)',
        color: slurmMode === 'slurm' ? 'var(--accent-text)' : 'var(--muted-foreground)',
      }}
    >
      {slurmMode}
    </span>
  ) : undefined

  const handlePlainChange = (k: string, v: string) =>
    setPlain(prev => ({ ...prev, [k]: v }))

  const handleSecretType = (k: string, v: string) =>
    setSecrets(prev => ({ ...prev, [k]: { typed: v, cleared: false } }))

  const handleSecretClear = (k: string) =>
    setSecrets(prev => ({ ...prev, [k]: { typed: '', cleared: true } }))

  const [mzAutoRetrain, setMzAutoRetrain] = useState(false)
  useEffect(() => {
    apiFetch<{ auto_retrain: boolean }>('/api/proxy/control_plane/modelzoo/config')
      .then(data => setMzAutoRetrain(data.auto_retrain))
      .catch(() => {})
  }, [])
  const [mzSaving, setMzSaving] = useState(false)

  const [exporting, setExporting] = useState(false)
  const [exportDone, setExportDone] = useState(false)
  const [exportError, setExportError] = useState<string | null>(null)

  const [importing, setImporting] = useState(false)
  const [importResult, setImportResult] = useState<{ imported: string[]; skipped: string[] } | null>(null)
  const [importError, setImportError] = useState<string | null>(null)
  const importInputRef = useRef<HTMLInputElement>(null)

  const handleImportEnv = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    e.target.value = ''
    setImporting(true)
    setImportResult(null)
    setImportError(null)
    try {
      const result = await importEnv(file)
      setImportResult(result)
      updateConfig.reset()
      await qc.invalidateQueries({ queryKey: ['config'] })
      setTimeout(() => setImportResult(null), 6000)
    } catch (err) {
      setImportError(err instanceof Error ? err.message : 'Import failed')
      setTimeout(() => setImportError(null), 5000)
    } finally {
      setImporting(false)
    }
  }

  const handleExportEnv = async () => {
    setExporting(true)
    setExportDone(false)
    setExportError(null)
    try {
      await exportEnv()
      setExportDone(true)
      setTimeout(() => setExportDone(false), 4000)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Export failed'
      setExportError(msg)
      setTimeout(() => setExportError(null), 4000)
    } finally {
      setExporting(false)
    }
  }

  const handleSave = () => {
    const payload: Record<string, string | null> = {}
    for (const [k, v] of Object.entries(plain)) {
      if (v !== (savedConfig?.[k] ?? '')) payload[k] = v
    }
    for (const [k, st] of Object.entries(secrets)) {
      if (st.cleared) payload[k] = null
      else if (st.typed !== '') payload[k] = st.typed
    }
    updateConfig.mutate(payload as Record<string, string>)
    setSecrets({})
  }

  return (
    <div className="p-6 space-y-6 max-w-3xl mx-auto">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Configuration</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Service endpoints, credentials, and runtime parameters.
          </p>
        </div>
        {!readOnly && (
          <button
            type="button"
            onClick={handleSave}
            disabled={updateConfig.isPending}
            className="inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-150 disabled:opacity-50 glow-primary"
            style={{
              background: 'oklch(0.64 0.20 265)',
              color: 'oklch(0.99 0 0)',
              border: '1px solid oklch(0.64 0.20 265 / 60%)',
            }}
          >
            <Save className="w-3.5 h-3.5" />
            {updateConfig.isPending ? 'Saving…' : 'Save'}
          </button>
        )}
      </div>

      {readOnly && (
        <Alert style={{
          background: 'oklch(0.78 0.18 55 / 10%)',
          border: '1px solid oklch(0.78 0.18 55 / 25%)',
        }}>
          <AlertDescription style={{ color: 'var(--warning-text)' }}>
            Read-only — sign in as admin to edit.
          </AlertDescription>
        </Alert>
      )}

      {updateConfig.isSuccess && (
        <Alert style={{
          background: 'oklch(0.72 0.18 155 / 10%)',
          border: '1px solid oklch(0.72 0.18 155 / 25%)',
        }}>
          <AlertDescription style={{ color: 'var(--success-text)' }}>
            Configuration saved successfully.
          </AlertDescription>
        </Alert>
      )}

      <div className="space-y-3">
        <EndpointsSection
          values={plain}
          onChange={handlePlainChange}
          readOnly={readOnly}
          services={services}
        />

        <CredentialsSection
          state={secrets}
          hasValueByKey={hasValueByKey}
          keysMeta={keysMeta}
          onType={handleSecretType}
          onClear={handleSecretClear}
          readOnly={readOnly}
        />

        <ApiTokenSection />

        {/* The `exa` configuration the dashboard's own CLI runs use (ADR 0119). */}
        <CliFlagGate quiet>
          <CliConfigSection />
        </CliFlagGate>

        <QuickActionsSection />

        <PlainSection
          title="Promotion Thresholds"
          icon={SlidersHorizontal}
          note="Reference only — pipeline reads from code"
          keys={THRESHOLD_KEYS}
          values={plain}
          onChange={handlePlainChange}
          readOnly={readOnly}
        />

        <GitLabSection
          gitlabUrl={plain['gitlab_url'] ?? ''}
          onGitlabUrlChange={v => handlePlainChange('gitlab_url', v)}
          projectId={plain['gitlab_project_id'] ?? ''}
          onProjectIdChange={v => handlePlainChange('gitlab_project_id', v)}
          secretState={secrets['gitlab_token']}
          hasToken={hasValueByKey['gitlab_token'] === true}
          tokenMeta={keysMeta?.find(m => m.key === 'gitlab_token')}
          onTokenType={v => handleSecretType('gitlab_token', v)}
          onTokenClear={() => handleSecretClear('gitlab_token')}
          readOnly={readOnly}
        />

        <ModelzooWebhookSection
          autoRetrain={mzAutoRetrain}
          onAutoRetrainChange={setMzAutoRetrain}
          webhookUrl={
            services?.['control_plane']?.url
              ? `${services['control_plane'].url}/webhooks/modelzoo/gitlab`
              : `http://${window.location.hostname}:18002/webhooks/modelzoo/gitlab`
          }
          onSave={async () => {
            setMzSaving(true)
            try {
              const token = getToken()
              const res = await fetch('/api/proxy/control_plane/modelzoo/config', {
                method: 'PUT',
                headers: {
                  'Content-Type': 'application/json',
                  ...(token ? { Authorization: `Bearer ${token}` } : {}),
                },
                body: JSON.stringify({ auto_retrain: mzAutoRetrain }),
              })
              if (!res.ok) {
                const msg = res.status === 401 || res.status === 403
                  ? 'Unauthorized'
                  : `API error ${res.status}`
                console.error('Failed to save ModelZoo webhook config:', msg)
              }
            } catch (e) {
              console.error('Failed to save ModelZoo webhook config', e)
            } finally {
              setMzSaving(false)
            }
          }}
          saving={mzSaving}
          readOnly={readOnly}
        />

        <PlainSection
          title="Slurm Adapter"
          icon={Server}
          keys={SLURM_KEYS}
          values={plain}
          onChange={handlePlainChange}
          readOnly={readOnly}
          badge={slurmBadge}
        />
      </div>

      {isAdmin() && (
        <div
          className="rounded-xl p-5 space-y-4"
          style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}
        >
          {/* Import from .env */}
          <div className="flex items-center justify-between gap-4">
            <div className="space-y-0.5">
              <p className="text-sm font-semibold">Import from .env</p>
              <p className="text-xs text-muted-foreground">
                Upload a <code>.env</code> or <code>.env.dashboard</code> file to populate config from environment variables.
              </p>
            </div>
            <div className="shrink-0">
              <input
                ref={importInputRef}
                type="file"
                accept=".env,.env.dashboard,text/plain"
                className="hidden"
                onChange={handleImportEnv}
              />
              <button
                onClick={() => importInputRef.current?.click()}
                disabled={importing}
                className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-opacity disabled:opacity-50"
                style={{
                  background: importError ? 'oklch(0.65 0.22 25)' : 'var(--surface-2)',
                  border: '1px solid var(--border-md)',
                  color: importError ? 'white' : 'var(--foreground)',
                }}
              >
                <Upload className="w-4 h-4" />
                {importing ? 'Importing…' : importError ? 'Failed' : 'Import .env'}
              </button>
            </div>
          </div>
          {importResult && (
            <div
              className="rounded-lg px-3 py-2.5 text-xs space-y-1"
              style={{
                background: 'oklch(0.72 0.18 155 / 10%)',
                border: '1px solid oklch(0.72 0.18 155 / 25%)',
                color: 'var(--success-text)',
              }}
            >
              <p className="font-medium">Imported {importResult.imported.length} key{importResult.imported.length !== 1 ? 's' : ''}</p>
              {importResult.imported.length > 0 && (
                <p className="text-muted-foreground font-mono">{importResult.imported.join(', ')}</p>
              )}
              {importResult.skipped.length > 0 && (
                <p className="text-muted-foreground">
                  Skipped {importResult.skipped.length} unknown: {importResult.skipped.slice(0, 5).join(', ')}
                  {importResult.skipped.length > 5 ? ` +${importResult.skipped.length - 5} more` : ''}
                </p>
              )}
            </div>
          )}
          {importError && (
            <p className="text-xs" style={{ color: 'oklch(0.65 0.22 25)' }}>{importError}</p>
          )}

          <div style={{ borderTop: '1px solid var(--border-sm)' }} />

          {/* Export / Apply Config */}
          <div className="flex items-center justify-between gap-4">
            <div className="space-y-0.5">
              <p className="text-sm font-semibold">Apply Config</p>
              <p className="text-xs text-muted-foreground">
                Downloads <code>.env.dashboard</code> — place in the repo root, then{' '}
                <code>docker-compose restart</code> affected services.
              </p>
            </div>
            <button
              onClick={handleExportEnv}
              disabled={exporting}
              className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-opacity disabled:opacity-50 shrink-0"
              style={{
                background: exportError ? 'oklch(0.65 0.22 25)' : 'oklch(0.64 0.20 265)',
                color: 'white',
              }}
            >
              <Download className="w-4 h-4" />
              {exporting ? 'Exporting…' : exportDone ? 'Downloaded ✓' : exportError ? 'Failed' : 'Apply Config'}
            </button>
          </div>
          {exportError && (
            <p className="text-xs" style={{ color: 'oklch(0.65 0.22 25)' }}>
              {exportError}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
