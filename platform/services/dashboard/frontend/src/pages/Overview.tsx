import { Link } from 'react-router-dom'
import { Server, Activity, Box, Database, Layers, GitBranch } from 'lucide-react'
import { ServiceCard } from '@/components/ServiceCard'
import { ArchitectureFlow } from '@/components/ArchitectureFlow'
import { useHealth, useModels, useModelRegistry, useModelzooStats } from '@/lib/api'
import type { ServiceStatus } from '@/lib/api'
import { Skeleton } from '@/components/ui/skeleton'
import { GrafanaPanel } from '@/components/GrafanaPanel'
import { useTheme } from '@/lib/theme'

const SERVICE_LABELS: Record<string, string> = {
  mlflow:        'MLflow',
  prefect:       'Prefect',
  ray_serve:     'Ray Serve',
  prometheus:    'Prometheus',
  grafana:       'Grafana',
  minio:         'MinIO',
  control_plane: 'Control Plane',
  postgres:      'PostgreSQL',
  loki:          'Loki',
  seanerbus:     'SeanerBUS Bridge',
  seanerbus_sim: 'SeanerBUS',
  jupyterhub:    'JupyterHub',
  dashboard:     'Dashboard',
  slurm:         'Slurm Adapter',
}

// ── Stat card ─────────────────────────────────────────────────────────────────

function StatCard({
  label,
  value,
  icon: Icon,
  accent,
  href,
}: {
  label: string
  value: number | string
  icon: React.ComponentType<{ className?: string; style?: React.CSSProperties }>
  accent?: 'green' | 'indigo' | 'amber'
  href?: string
}) {
  const colors = {
    green:  { bg: 'oklch(0.72 0.18 155 / 12%)', border: 'oklch(0.72 0.18 155 / 25%)', icon: 'var(--success-text)', text: 'var(--success-text)' },
    indigo: { bg: 'oklch(0.64 0.20 265 / 12%)', border: 'oklch(0.64 0.20 265 / 25%)', icon: 'var(--accent-text)', text: 'var(--accent-text)' },
    amber:  { bg: 'oklch(0.78 0.18 80 / 12%)',  border: 'oklch(0.78 0.18 80 / 25%)',  icon: 'var(--warning-text)',  text: 'var(--warning-text)'  },
  }
  const c = accent ? colors[accent] : {
    bg: 'var(--surface-2)',
    border: 'var(--border)',
    icon: 'var(--subtle-text)',
    text: 'var(--foreground)',
  }

  const inner = (
    <>
      <div
        className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
        style={{ background: c.bg, border: `1px solid ${c.border}` }}
      >
        <Icon className="w-4 h-4" style={{ color: c.icon } as React.CSSProperties} />
      </div>
      <div>
        <p className="text-2xl font-bold leading-none" style={{ color: c.text }}>{value}</p>
        <p className="text-xs text-muted-foreground mt-0.5">{label}</p>
      </div>
    </>
  )

  const base = 'rounded-xl p-4 flex items-center gap-3'
  const style = { background: c.bg, border: `1px solid ${c.border}` }

  if (href) {
    return (
      <Link to={href} className={`${base} transition-opacity hover:opacity-80`} style={style}>
        {inner}
      </Link>
    )
  }
  return <div className={base} style={style}>{inner}</div>
}

function StatusSummaryBar({ services }: { services: [string, { status: ServiceStatus; url: string }][] }) {
  const online   = services.filter(([, s]) => s.status === 'ok').length
  const degraded = services.filter(([, s]) => s.status === 'degraded').length
  const down     = services.filter(([, s]) => s.status === 'down').length
  const total    = services.length
  if (total === 0) return null

  return (
    <div className="flex items-center gap-3 text-xs text-muted-foreground">
      {online   > 0 && <span className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-emerald-500 inline-block" />{online} online</span>}
      {degraded > 0 && <span className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-amber-500 inline-block" />{degraded} degraded</span>}
      {down     > 0 && <span className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-red-500 inline-block" />{down} offline</span>}
    </div>
  )
}

export function Overview() {
  const { data, isLoading, error } = useHealth()
  const { data: models } = useModels()
  const { data: registry } = useModelRegistry()
  const { data: zoo } = useModelzooStats()
  const { theme } = useTheme()
  const grafanaTheme = theme === 'day' ? 'light' : 'dark'

  const services = data ? (Object.entries(data.services) as [string, { status: ServiceStatus; url: string }][]) : []
  const onlineCount  = services.filter(([, s]) => s.status === 'ok').length

  // GitLab-sourced counts take precedence; fall back to registry-derived counts
  const modelsCount   = zoo?.configured ? (zoo.models_count   ?? '—') : (registry?.length ?? '—')
  const datasetsCount = zoo?.configured ? (zoo.datasets_count ?? '—') : '—'

  const servicesMap = Object.fromEntries(services) as Record<string, { status: string; url: string }>

  const lastCommit = zoo?.configured ? zoo.last_commit : null

  return (
    <div className="p-6 space-y-8 max-w-5xl mx-auto">
      {/* ── Hero ── */}
      <div
        className="relative overflow-hidden rounded-2xl p-8"
        style={{
          background: 'linear-gradient(135deg, var(--surface-1) 0%, var(--surface-0) 60%, oklch(0.64 0.20 265 / 8%) 100%)',
          border: '1px solid oklch(0.64 0.20 265 / 20%)',
        }}
      >
        <div
          className="absolute top-0 right-0 w-72 h-72 rounded-full pointer-events-none"
          style={{
            background: 'radial-gradient(circle, oklch(0.64 0.20 265 / 10%) 0%, transparent 70%)',
            transform: 'translate(30%, -30%)',
          }}
        />
        <div className="relative space-y-3">
          <div className="flex items-center gap-3">
            <div
              className="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium"
              style={{
                background: 'oklch(0.72 0.18 155 / 12%)',
                border: '1px solid oklch(0.72 0.18 155 / 30%)',
                color: 'var(--success-text)',
              }}
            >
              <span className="relative flex h-1.5 w-1.5">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-emerald-500" />
              </span>
              Platform Active
            </div>
            {data?.checked_at && (
              <span className="text-xs text-muted-foreground">
                Last checked {new Date(data.checked_at).toLocaleTimeString()}
              </span>
            )}
          </div>

          <h1 className="text-4xl font-bold tracking-tight gradient-text">
            ExaMLOps Platform
          </h1>
          <p className="text-muted-foreground text-sm max-w-lg leading-relaxed">
            End-to-end MLOps platform for HPC systems — auto-discovery training pipelines,
            model versioning, Slurm orchestration, and multi-model serving.
          </p>

          {lastCommit && (
            <div className="flex items-center gap-2 pt-1">
              <GitBranch className="w-3.5 h-3.5 shrink-0" style={{ color: 'var(--subtle-text)' }} />
              <span className="text-xs text-muted-foreground font-mono truncate">
                {lastCommit.sha}
              </span>
              <span className="text-xs text-muted-foreground truncate max-w-xs">
                {lastCommit.message}
              </span>
              {lastCommit.committed_date && (
                <span className="text-xs text-muted-foreground/60 shrink-0 ml-auto">
                  {new Date(lastCommit.committed_date).toLocaleDateString()}
                </span>
              )}
            </div>
          )}
        </div>
      </div>

      {/* ── Stats ── */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        <StatCard label="Services"       value={services.length || '—'}        icon={Server}   />
        <StatCard label="Online"         value={isLoading ? '…' : onlineCount} icon={Activity} accent="green" />
        <StatCard label="Models Loaded"  value={models?.length ?? '—'}         icon={Box}      accent="indigo" href="/models" />
        <StatCard label="ModelZoo"       value={modelsCount}                   icon={Layers}   accent="amber"  href="/models" />
        <StatCard label="Datasets"       value={datasetsCount}                 icon={Database} href="/datasets" />
      </div>

      {/* ── Architecture Flowchart ── */}
      <ArchitectureFlow services={servicesMap} />

      {/* ── Live metrics (Grafana embed, F5) ── */}
      <div className="space-y-3">
        <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
          Live Metrics
        </h2>
        <GrafanaPanel
          name="overview.online"
          baseUrl={servicesMap.grafana?.url}
          theme={grafanaTheme}
          timeRange={{ from: 'now-6h', to: 'now' }}
          title="Platform metrics"
        />
      </div>

      {/* ── Service Health ── */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
            Service Health
          </h2>
          <StatusSummaryBar services={services} />
        </div>

        {error && (
          <p className="text-sm rounded-lg px-4 py-3"
            style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
            Failed to load health status.
          </p>
        )}

        {isLoading && (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3" aria-label="Loading services">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-20 w-full" />
            ))}
          </div>
        )}

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {services.map(([key, svc]) => (
            <ServiceCard
              key={key}
              name={SERVICE_LABELS[key] ?? key}
              url={svc.url}
              status={svc.status}
            />
          ))}
        </div>
      </div>
    </div>
  )
}
