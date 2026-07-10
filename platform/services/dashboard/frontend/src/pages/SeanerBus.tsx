import { useState } from 'react'
import { RefreshCw, Zap, Activity, RotateCcw, Server, Settings, Copy, Check, ScrollText } from 'lucide-react'
import { LogPanel } from '@/components/LogPanel'
import { Skeleton } from '@/components/ui/skeleton'
import { useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { useSeanerbusConfig, useSeanerbusStatus, useSeanerbusModelUuids, useSeanerbusGrafanaPanels } from '@/lib/api'

// ── Types ────────────────────────────────────────────────────────────────────

interface StatCardProps {
  label: string
  value: number | string
  icon: React.ComponentType<{ className?: string; style?: React.CSSProperties }>
  accent?: 'green' | 'indigo' | 'amber' | 'red'
}

// ── Constants ────────────────────────────────────────────────────────────────

const MODE_LABELS: Record<string, string> = {
  both:   'Pub/Sub + Req/Res',
  pubsub: 'Pub/Sub only',
  reqres: 'Req/Res only',
}

const CONFIG_LABELS: Record<string, string> = {
  seanerbus_host:             'Host',
  seanerbus_port:             'Port',
  seanerbus_mode:             'Mode',
  seanerbus_job_topic_uuid:   'Job Topic UUID',
  seanerbus_result_topic_uuid:'Result Topic UUID',
  seanerbus_inference_uuid:   'Inference Handler UUID',
  seanerbus_retrain_uuid:     'Retrain Handler UUID',
  seanerbus_default_model:    'Default Model',
  seanerbus_default_alias:    'Default Alias',
  seanerbus_bridge_status_url:'Bridge Status URL',
}

// ── Sub-components ───────────────────────────────────────────────────────────

function StatCard({ label, value, icon: Icon, accent }: StatCardProps) {
  const COLORS = {
    green:  { bg: 'oklch(0.72 0.18 155 / 12%)', border: 'oklch(0.72 0.18 155 / 25%)', icon: 'var(--success-text)',  text: 'var(--success-text)'  },
    indigo: { bg: 'oklch(0.64 0.20 265 / 12%)', border: 'oklch(0.64 0.20 265 / 25%)', icon: 'var(--accent-text)',   text: 'var(--accent-text)'   },
    amber:  { bg: 'oklch(0.78 0.18 80  / 12%)', border: 'oklch(0.78 0.18 80  / 25%)', icon: 'var(--warning-text)', text: 'var(--warning-text)'  },
    red:    { bg: 'oklch(0.66 0.22 25  / 12%)', border: 'oklch(0.66 0.22 25  / 25%)', icon: 'var(--error-text)',   text: 'var(--error-text)'    },
  }
  const c = accent ? COLORS[accent] : {
    bg: 'var(--surface-2)', border: 'var(--border)', icon: 'var(--subtle-text)', text: 'var(--foreground)',
  }
  return (
    <div className="rounded-xl p-4 flex items-center gap-3" style={{ background: c.bg, border: `1px solid ${c.border}` }}>
      <div className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
        style={{ background: c.bg, border: `1px solid ${c.border}` }}>
        <Icon className="w-4 h-4" style={{ color: c.icon } as React.CSSProperties} />
      </div>
      <div>
        <p className="text-2xl font-bold leading-none" style={{ color: c.text }}>{value}</p>
        <p className="text-xs text-muted-foreground mt-0.5">{label}</p>
      </div>
    </div>
  )
}

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard.writeText(value)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <button
      onClick={copy}
      title="Copy UUID"
      className="rounded p-1 transition-opacity hover:opacity-70"
      style={{ background: 'var(--surface-2)', border: '1px solid var(--border)' }}
    >
      {copied
        ? <Check className="w-3.5 h-3.5" style={{ color: 'var(--success-text)' }} />
        : <Copy className="w-3.5 h-3.5" style={{ color: 'var(--muted-foreground)' }} />}
    </button>
  )
}

// ── SVG flow diagram ─────────────────────────────────────────────────────────

function BridgeFlowDiagram({ online }: { online: boolean }) {
  const NW = 140; const NH = 40
  const nodes = [
    { x: 10,  y: 60, label: 'SeanerBUS',  sub: 'Cap\'n\'Proto :5398' },
    { x: 200, y: 60, label: 'Bridge',      sub: 'status :8003' },
    { x: 390, y: 60, label: 'Ray Serve',   sub: 'inference :8001' },
    { x: 580, y: 60, label: 'MLflow',      sub: 'registry :5000' },
  ]
  const bridgeColor = online
    ? { border: '#10b981', fill: 'oklch(0.72 0.18 155 / 15%)', dot: '#10b981' }
    : { border: '#ef4444', fill: 'oklch(0.66 0.22 25 / 15%)',  dot: '#ef4444' }
  const defaultColor = { border: 'oklch(0.48 0.012 260)', fill: 'oklch(0.50 0.012 260 / 10%)', dot: 'oklch(0.52 0.012 260)' }

  const nodeColors = [defaultColor, bridgeColor, defaultColor, defaultColor]

  const edges = [
    { x1: 10 + NW, y1: 60 + NH / 2, x2: 200, y2: 60 + NH / 2, label: 'HpcJobV1', ly: 52 },
    { x1: 200 + NW, y1: 60 + NH / 2, x2: 390, y2: 60 + NH / 2, label: 'POST /predict', ly: 52 },
    { x1: 390 + NW, y1: 60 + NH / 2, x2: 580, y2: 60 + NH / 2, label: 'alias → version', ly: 52 },
  ]

  // Return arrow (below)
  const returnEdges = [
    { x1: 390, y1: 60 + NH / 2 + 20, x2: 200 + NW, y2: 60 + NH / 2 + 20, label: 'prediction', ly: 122 },
    { x1: 200, y1: 60 + NH / 2 + 20, x2: 10 + NW,  y2: 60 + NH / 2 + 20, label: 'HpcInferenceResV1', ly: 122 },
  ]

  return (
    <div className="rounded-xl overflow-hidden" style={{ background: 'var(--surface-deep)', border: '1px solid var(--border-sm)' }}>
      <div style={{ overflowX: 'auto', padding: '16px 20px 12px' }}>
        <svg width={760} height={160} viewBox="0 0 760 160" style={{ display: 'block', minWidth: 720 }}>
          <defs>
            <marker id="sbah" markerWidth="9" markerHeight="7" refX="8" refY="3.5" orient="auto" markerUnits="userSpaceOnUse">
              <path d="M 0 0 L 9 3.5 L 0 7 z" fill="var(--muted-foreground)" opacity="0.6" />
            </marker>
            <marker id="sbah-ret" markerWidth="9" markerHeight="7" refX="8" refY="3.5" orient="auto" markerUnits="userSpaceOnUse">
              <path d="M 0 0 L 9 3.5 L 0 7 z" fill="var(--subtle-text)" opacity="0.45" />
            </marker>
          </defs>

          {/* Forward edges */}
          {edges.map((e, i) => (
            <g key={i}>
              <line x1={e.x1} y1={e.y1} x2={e.x2} y2={e.y2} strokeWidth="1.5" strokeDasharray="5 3" markerEnd="url(#sbah)" style={{ stroke: 'var(--border-md)' }} />
              <text x={(e.x1 + e.x2) / 2} y={e.ly} fontSize="9" fontFamily="'Geist Variable',sans-serif" textAnchor="middle" style={{ fill: 'var(--faint-text)' }}>{e.label}</text>
            </g>
          ))}

          {/* Return path line */}
          <line x1={10 + NW} y1={60 + NH / 2 + 20} x2={390} y2={60 + NH / 2 + 20} strokeWidth="1" strokeDasharray="3 3" style={{ stroke: 'var(--border-sm)' }} opacity="0.5" />
          {returnEdges.map((e, i) => (
            <g key={i}>
              <line x1={e.x1} y1={e.y1} x2={e.x2} y2={e.y2} strokeWidth="1" strokeDasharray="3 3" markerEnd="url(#sbah-ret)" style={{ stroke: 'var(--subtle-text)' }} opacity="0.5" />
              <text x={(e.x1 + e.x2) / 2} y={e.ly} fontSize="9" fontFamily="'Geist Variable',sans-serif" textAnchor="middle" style={{ fill: 'var(--faint-text)' }} opacity="0.6">{e.label}</text>
            </g>
          ))}

          {/* Nodes */}
          {nodes.map((n, i) => {
            const c = nodeColors[i]
            return (
              <g key={i}>
                <rect x={n.x} y={n.y} width={NW} height={NH} rx={8} fill={c.fill} stroke={c.border} strokeWidth="1.5" />
                <circle cx={n.x + 12} cy={n.y + NH / 2 - 4} r={3} fill={c.dot} opacity={0.9} />
                <text x={n.x + 22} y={n.y + NH / 2 - 4} dominantBaseline="central" fontSize="12" fontWeight="600" fontFamily="'Geist Variable',sans-serif" style={{ fill: 'var(--foreground)' }}>{n.label}</text>
                <text x={n.x + 22} y={n.y + NH / 2 + 10} dominantBaseline="central" fontSize="9" fontFamily="'Geist Variable',sans-serif" style={{ fill: 'var(--subtle-text)' }}>{n.sub}</text>
              </g>
            )
          })}
        </svg>
      </div>
      <div className="flex items-center gap-4 px-5 pb-3 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full inline-block bg-emerald-500" />Bridge online</span>
        <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full inline-block bg-red-500" />Bridge offline</span>
        <span className="ml-auto opacity-50">dashed = data flow · solid below = response path</span>
      </div>
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function SeanerBus() {
  const qc = useQueryClient()
  const { data: status, isFetching: statusFetching } = useSeanerbusStatus()
  const { data: config, isFetching: configFetching } = useSeanerbusConfig()
  const { data: modelUuids, isFetching: uuidsFetching } = useSeanerbusModelUuids()
  const { data: grafanaPanels, isFetching: panelsFetching } = useSeanerbusGrafanaPanels()
  const loading = statusFetching || configFetching || uuidsFetching || panelsFetching
  const token = typeof window !== 'undefined' ? localStorage.getItem('auth_token') : null
  const online  = status?.reachable ?? false

  const refresh = () => qc.invalidateQueries({ queryKey: ['seanerbus'] })

  const errorRate = (m: { inferences: number; errors: number }) =>
    m.inferences > 0 ? `${((m.errors / m.inferences) * 100).toFixed(1)}%` : '—'

  const configSet = config && Object.keys(config).length > 0

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">

      {/* ── Hero ── */}
      <div
        className="relative overflow-hidden rounded-2xl p-8"
        style={{
          background: online
            ? 'linear-gradient(135deg, var(--surface-1) 0%, var(--surface-0) 60%, oklch(0.72 0.18 155 / 8%) 100%)'
            : 'linear-gradient(135deg, var(--surface-1) 0%, var(--surface-0) 60%, oklch(0.66 0.22 25 / 6%) 100%)',
          border: `1px solid ${online ? 'oklch(0.72 0.18 155 / 20%)' : 'oklch(0.66 0.22 25 / 18%)'}`,
        }}
      >
        <div
          className="absolute top-0 right-0 w-64 h-64 rounded-full pointer-events-none"
          style={{
            background: online
              ? 'radial-gradient(circle, oklch(0.72 0.18 155 / 10%) 0%, transparent 70%)'
              : 'radial-gradient(circle, oklch(0.66 0.22 25 / 8%) 0%, transparent 70%)',
            transform: 'translate(30%, -30%)',
          }}
        />
        <div className="relative flex items-start justify-between gap-4">
          <div className="space-y-3">
            {/* Status pill */}
            <div
              className="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium"
              style={online
                ? { background: 'oklch(0.72 0.18 155 / 12%)', border: '1px solid oklch(0.72 0.18 155 / 30%)', color: 'var(--success-text)' }
                : { background: 'oklch(0.66 0.22 25 / 10%)',  border: '1px solid oklch(0.66 0.22 25 / 25%)',  color: 'var(--error-text)' }}
            >
              {online ? (
                <>
                  <span className="relative flex h-1.5 w-1.5">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                    <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-emerald-500" />
                  </span>
                  Bridge Online
                </>
              ) : (
                <>
                  <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-red-500" />
                  Bridge Offline
                </>
              )}
            </div>

            <h1 className="text-3xl font-bold tracking-tight gradient-text">SeanerBUS Bridge</h1>
            <p className="text-muted-foreground text-sm max-w-lg leading-relaxed">
              Connects HPC workload messages (Cap'n'Proto) to Ray Serve inference.
              Receives <code className="text-xs px-1 py-0.5 rounded" style={{ background: 'var(--surface-2)' }}>HpcJobV1</code> and
              returns <code className="text-xs px-1 py-0.5 rounded" style={{ background: 'var(--surface-2)' }}>HpcInferenceResV1</code> results.
              {status?.health?.mode && (
                <span className="ml-2 font-medium" style={{ color: 'var(--accent-text)' }}>
                  Mode: {MODE_LABELS[status.health.mode] ?? status.health.mode}
                </span>
              )}
            </p>

            {!online && status?.error && (
              <p className="text-xs rounded-lg px-3 py-2 max-w-md"
                style={{ background: 'oklch(0.66 0.22 25 / 10%)', border: '1px solid oklch(0.66 0.22 25 / 20%)', color: 'var(--error-text)' }}>
                {status.error}
              </p>
            )}
          </div>

          <button
            className="shrink-0 flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium transition-opacity hover:opacity-80"
            style={{ background: 'var(--surface-2)', border: '1px solid var(--border)', color: 'var(--foreground)' }}
            onClick={refresh}
            disabled={loading}
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>

      {/* ── Stats ── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard
          label="Bridge Status"
          value={!status ? '…' : online ? 'Online' : 'Offline'}
          icon={Zap}
          accent={!status ? undefined : online ? 'green' : 'red'}
        />
        <StatCard
          label="Total Inferences"
          value={status?.stats?.inferences_total ?? '—'}
          icon={Activity}
          accent={online ? 'indigo' : undefined}
        />
        <StatCard
          label="Drift Retrains"
          value={status?.stats?.retrains_total ?? '—'}
          icon={RotateCcw}
          accent={online && (status?.stats?.retrains_total ?? 0) > 0 ? 'amber' : undefined}
        />
        <StatCard
          label="Models Tracked"
          value={status?.stats ? Object.keys(status.stats.per_model).length : '—'}
          icon={Server}
          accent={online ? 'indigo' : undefined}
        />
      </div>

      {/* ── Model UUIDs ── */}
      <div className="space-y-2">
        <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
          Model UUIDs
        </h2>
        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border-sm)' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border-sm)' }}>
                {['Model', 'SeanerBUS UUID'].map(h => (
                  <th key={h} style={{ padding: '10px 16px', textAlign: 'left', fontSize: '11px', fontWeight: 600, letterSpacing: '0.06em', color: 'var(--muted-foreground)', textTransform: 'uppercase' }}>{h}</th>
                ))}
                <th style={{ width: 44 }} />
              </tr>
            </thead>
            <tbody>
              {modelUuids && Object.entries(modelUuids).map(([model, uid], i, arr) => (
                <tr key={model} style={{ borderBottom: i < arr.length - 1 ? '1px solid var(--border-sm)' : 'none', background: 'var(--surface-0)' }}>
                  <td style={{ padding: '10px 16px' }}>
                    <code style={{ fontSize: '0.8rem', padding: '2px 6px', borderRadius: '4px', background: 'var(--surface-2)' }}>{model}</code>
                  </td>
                  <td style={{ padding: '10px 16px', fontFamily: 'monospace', fontSize: '0.82rem', color: uid ? 'var(--foreground)' : 'var(--faint-text)' }}>
                    {uid ?? (
                      <span style={{ color: 'var(--warning-text)', fontFamily: 'inherit', fontSize: '0.8rem' }}>
                        not assigned — run <code style={{ fontSize: '0.78rem' }}>exa seanerbus init-uuids</code>
                      </span>
                    )}
                  </td>
                  <td style={{ padding: '10px 8px', textAlign: 'right' }}>
                    {uid && <CopyButton value={uid} />}
                  </td>
                </tr>
              ))}
              {!modelUuids && (
                <tr>
                  <td colSpan={3} style={{ padding: '12px 16px' }}>
                    <div className="space-y-2" aria-label="Loading UUIDs">
                      {Array.from({ length: 3 }).map((_, i) => (
                        <Skeleton key={i} className="h-6 w-full" />
                      ))}
                    </div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── Live Metrics ── */}
      <div className="space-y-2">
        <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
          Live Metrics
        </h2>
        {grafanaPanels ? (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            {(
              [
                { id: grafanaPanels.panels.inference_rate, title: 'Inference Rate' },
                { id: grafanaPanels.panels.error_rate,     title: 'Error Rate' },
                { id: grafanaPanels.panels.latency,        title: 'Latency p50 / p99' },
              ] as const
            ).map(({ id, title }) => (
              <div
                key={id}
                className="rounded-xl overflow-hidden"
                style={{ border: '1px solid var(--border-sm)', background: 'var(--surface-deep)' }}
              >
                <p className="text-xs font-medium px-3 pt-2 pb-1" style={{ color: 'var(--muted-foreground)' }}>
                  {title}
                </p>
                <iframe
                  src={`${grafanaPanels.grafana_url}/d-solo/${grafanaPanels.dashboard_uid}/seanerbus-bridge?panelId=${id}&orgId=1&theme=dark&kiosk`}
                  width="100%"
                  height="180"
                  style={{ border: 'none', display: 'block' }}
                  title={title}
                />
              </div>
            ))}
          </div>
        ) : (
          <div
            className="rounded-xl p-5 flex items-center gap-3"
            style={{ background: 'var(--surface-1)', border: '1px solid var(--border-sm)' }}
          >
            <Activity className="w-4 h-4 shrink-0" style={{ color: 'var(--muted-foreground)' }} />
            <p className="text-sm" style={{ color: 'var(--muted-foreground)' }}>
              Start the bridge and monitoring stack to see live metrics.
            </p>
          </div>
        )}
      </div>

      {/* ── Flow diagram ── */}
      <div className="space-y-2">
        <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
          Inference Flow
        </h2>
        <BridgeFlowDiagram online={online} />
      </div>

      {/* ── Per-model stats (only when bridge is online and has traffic) ── */}
      {online && status?.stats && Object.keys(status.stats.per_model).length > 0 && (
        <div className="space-y-2">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
            Per-Model Traffic
          </h2>
          <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border-sm)' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border-sm)' }}>
                  {['Model', 'Inferences', 'Errors', 'Error Rate'].map(h => (
                    <th key={h} style={{ padding: '10px 16px', textAlign: 'left', fontSize: '11px', fontWeight: 600, letterSpacing: '0.06em', color: 'var(--muted-foreground)', textTransform: 'uppercase' }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {Object.entries(status.stats.per_model).map(([model, m], i, arr) => (
                  <tr key={model} style={{ borderBottom: i < arr.length - 1 ? '1px solid var(--border-sm)' : 'none', background: 'var(--surface-0)' }}>
                    <td style={{ padding: '10px 16px' }}>
                      <code style={{ fontSize: '0.8rem', padding: '2px 6px', borderRadius: '4px', background: 'var(--surface-2)' }}>{model}</code>
                    </td>
                    <td style={{ padding: '10px 16px', fontSize: '0.875rem', color: 'var(--foreground)' }}>{m.inferences}</td>
                    <td style={{ padding: '10px 16px', fontSize: '0.875rem', color: m.errors > 0 ? 'var(--error-text)' : 'var(--muted-foreground)' }}>{m.errors}</td>
                    <td style={{ padding: '10px 16px', fontSize: '0.875rem', color: m.errors > 0 ? 'var(--warning-text)' : 'var(--success-text)' }}>{errorRate(m)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Config ── */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
            Configuration
          </h2>
          <Link
            to="/config"
            className="flex items-center gap-1 text-xs font-medium transition-opacity hover:opacity-80"
            style={{ color: 'var(--accent-text)' }}
          >
            <Settings className="w-3 h-3" />
            Edit in Config
          </Link>
        </div>

        {configSet ? (
          <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border-sm)' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <tbody>
                {Object.entries(CONFIG_LABELS).map(([key, label], i, arr) => (
                  <tr key={key} style={{ borderBottom: i < arr.length - 1 ? '1px solid var(--border-sm)' : 'none', background: i % 2 === 0 ? 'var(--surface-0)' : 'var(--surface-1)' }}>
                    <td style={{ padding: '9px 16px', fontSize: '0.8rem', color: 'var(--muted-foreground)', width: '40%', fontWeight: 500 }}>{label}</td>
                    <td style={{ padding: '9px 16px' }}>
                      {config![key]
                        ? <code style={{ fontSize: '0.78rem', padding: '2px 6px', borderRadius: '4px', background: 'var(--surface-2)', color: 'var(--foreground)' }}>{config![key]}</code>
                        : <span style={{ fontSize: '0.8rem', color: 'var(--faint-text)' }}>not set</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div
            className="rounded-xl p-5 flex items-start gap-3"
            style={{ background: 'oklch(0.64 0.20 265 / 8%)', border: '1px solid oklch(0.64 0.20 265 / 20%)' }}
          >
            <Settings className="w-4 h-4 mt-0.5 shrink-0" style={{ color: 'var(--accent-text)' }} />
            <div>
              <p className="text-sm font-medium" style={{ color: 'var(--foreground)' }}>No SeanerBUS configuration</p>
              <p className="text-xs text-muted-foreground mt-0.5">
                Set host, port, UUIDs and mode in the{' '}
                <Link to="/config" style={{ color: 'var(--accent-text)' }}>Config page</Link>{' '}
                (admin role required) to connect the bridge.
              </p>
            </div>
          </div>
        )}
      </div>

      {/* ── Bridge Logs ── */}
      <div className="space-y-2">
        <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest flex items-center gap-1.5">
          <ScrollText className="w-3.5 h-3.5" />
          Bridge Logs
        </h2>
        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border-sm)' }}>
          <LogPanel containerName="seanerbus-bridge" token={token} />
        </div>
      </div>
    </div>
  )
}
