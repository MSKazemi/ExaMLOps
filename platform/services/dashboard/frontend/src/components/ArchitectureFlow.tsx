import { useMemo, useState } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  Handle,
  Position,
  MarkerType,
  type Node,
  type Edge,
  type NodeProps,
} from '@xyflow/react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import '@xyflow/react/dist/style.css'

// ── Status palette ────────────────────────────────────────────────────────────

type SKey = 'ok' | 'degraded' | 'down' | 'unknown'

const STATUS_COLORS: Record<SKey, { border: string; fill: string; dot: string }> = {
  ok:       { border: '#10b981', fill: 'oklch(0.72 0.18 155 / 12%)', dot: '#10b981' },
  degraded: { border: '#f59e0b', fill: 'oklch(0.78 0.18 80 / 14%)',  dot: '#f59e0b' },
  down:     { border: '#ef4444', fill: 'oklch(0.66 0.22 25 / 16%)',  dot: '#ef4444' },
  unknown:  { border: 'oklch(0.48 0.012 260)', fill: 'oklch(0.50 0.012 260 / 10%)', dot: 'oklch(0.52 0.012 260)' },
}

const NODE_W = 172
const NODE_H = 48

const HANDLE_STYLE = { opacity: 0, width: 1, height: 1, minWidth: 0, minHeight: 0, border: 'none' } as const
const SIDES = [
  ['top', Position.Top],
  ['right', Position.Right],
  ['bottom', Position.Bottom],
  ['left', Position.Left],
] as const

// Edges reference these handle ids so they always attach to a node border.
function StatusNode({ data }: NodeProps) {
  const status = (data.status as SKey) ?? 'unknown'
  const label = data.label as string
  const c = STATUS_COLORS[status]
  return (
    <div
      style={{
        width: NODE_W,
        height: NODE_H,
        borderRadius: 10,
        background: c.fill,
        border: `1.5px solid ${c.border}`,
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '0 12px',
        fontFamily: "'Geist Variable', sans-serif",
      }}
    >
      {SIDES.map(([id, pos]) => (
        <Handle key={`t-${id}`} id={`t-${id}`} type="target" position={pos} style={HANDLE_STYLE} isConnectable={false} />
      ))}
      {SIDES.map(([id, pos]) => (
        <Handle key={`s-${id}`} id={`s-${id}`} type="source" position={pos} style={HANDLE_STYLE} isConnectable={false} />
      ))}
      <span style={{ width: 7, height: 7, borderRadius: '50%', background: c.dot, flexShrink: 0 }} />
      <span style={{ fontSize: 12.5, fontWeight: 500, color: 'var(--foreground)', flex: 1, whiteSpace: 'nowrap' }}>
        {label}
      </span>
      {status !== 'unknown' && (
        <span style={{ fontSize: 9, color: c.border, opacity: 0.85 }}>{status}</span>
      )}
    </div>
  )
}

function GroupLabel({ data }: NodeProps) {
  return (
    <div
      style={{
        fontSize: 9,
        fontWeight: 600,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        whiteSpace: 'nowrap',
        color: 'var(--subtle-text)',
        fontFamily: "'Geist Variable', sans-serif",
        pointerEvents: 'none',
      }}
    >
      {data.label as string}
    </div>
  )
}

const nodeTypes = { status: StatusNode, groupLabel: GroupLabel }

// ── Layout ──────────────────────────────────────────────────────────────────
// Node ids match the keys of the /health services map so live status colours them.

const NODE_DEFS: { id: string; label: string; x: number; y: number }[] = [
  { id: 'seanerbus_sim', label: 'SeanerBUS',         x: 0,    y: 60  },
  { id: 'seanerbus',     label: 'SeanerBUS Bridge', x: 0,    y: 200 },
  { id: 'jupyterhub',    label: 'JupyterHub',       x: 0,    y: 440 },
  { id: 'control_plane', label: 'Control Plane',    x: 285,  y: 110 },
  { id: 'dashboard',     label: 'Dashboard',        x: 285,  y: 440 },
  { id: 'prefect',       label: 'Prefect',          x: 570,  y: 20  },
  { id: 'slurm',         label: 'Slurm Adapter',    x: 570,  y: 140 },
  { id: 'ray_serve',     label: 'Ray Serve',        x: 570,  y: 300 },
  { id: 'mlflow',        label: 'MLflow',           x: 855,  y: 80  },
  { id: 'prometheus',    label: 'Prometheus',       x: 855,  y: 300 },
  { id: 'loki',          label: 'Loki',             x: 855,  y: 420 },
  { id: 'postgres',      label: 'PostgreSQL',       x: 1140, y: 20  },
  { id: 'minio',         label: 'MinIO',            x: 1140, y: 150 },
  { id: 'grafana',       label: 'Grafana',          x: 1140, y: 300 },
]

const GROUP_DEFS: { id: string; label: string; x: number; y: number }[] = [
  { id: 'g-clients',    label: 'Clients',            x: 4,    y: 20  },
  { id: 'g-control',    label: 'Control',            x: 289,  y: 72  },
  { id: 'g-training',   label: 'Training',           x: 574,  y: -18 },
  { id: 'g-storage',    label: 'Registry & Storage', x: 859,  y: 42  },
  { id: 'g-serving',    label: 'Serving',            x: 574,  y: 262 },
  { id: 'g-monitoring', label: 'Monitoring',         x: 859,  y: 262 },
  { id: 'g-access',     label: 'Access',             x: 4,    y: 402 },
]

// [source, target, label, sourceHandle, targetHandle, dashed]
const EDGE_DEFS: [string, string, string, string, string, boolean][] = [
  // Primary data / control flow
  ['seanerbus_sim', 'seanerbus',     'Cap\'n\'Proto TCP', 's-right',  't-left',   false],
  ['seanerbus',     'control_plane', 'drift → retrain', 's-right',  't-left',   false],
  ['control_plane', 'prefect',       'schedule run',    's-right',  't-left',   false],
  ['prefect',       'slurm',         'submit job',      's-bottom', 't-top',    false],
  ['slurm',         'mlflow',        'log + register',  's-right',  't-left',   false],
  ['mlflow',        'postgres',      'metadata',        's-right',  't-left',   false],
  ['mlflow',        'minio',         'artifacts',       's-right',  't-left',   false],
  ['mlflow',        'ray_serve',     'load model',      's-bottom', 't-top',    false],
  ['seanerbus',     'ray_serve',     'infer-pipeline',  's-right',  't-left',   false],
  // Observability / proxy
  ['ray_serve',     'prometheus',    'metrics',         's-right',  't-left',   true],
  ['control_plane', 'prometheus',    'metrics',         's-bottom', 't-top',    true],
  ['prometheus',    'grafana',       'dashboards',      's-right',  't-left',   true],
  ['loki',          'grafana',       'logs',            's-right',  't-bottom', true],
  ['jupyterhub',    'mlflow',        'experiments',     's-right',  't-bottom', true],
  ['dashboard',     'control_plane', 'proxy / observe', 's-top',    't-bottom', true],
]

function buildEdges(): Edge[] {
  return EDGE_DEFS.map(([source, target, label, sh, th, dashed]) => ({
    id: `${source}-${target}`,
    source,
    target,
    sourceHandle: sh,
    targetHandle: th,
    label,
    type: 'smoothstep',
    markerEnd: {
      type: MarkerType.ArrowClosed,
      width: 15,
      height: 15,
      color: dashed ? 'var(--faint-text)' : 'var(--muted-foreground)',
    },
    style: {
      stroke: dashed ? 'var(--faint-text)' : 'var(--border-md)',
      strokeWidth: 1.5,
      strokeDasharray: dashed ? '5 4' : undefined,
    },
    labelStyle: { fontSize: 9, fill: 'var(--faint-text)', fontFamily: "'Geist Variable', sans-serif" },
    labelBgStyle: { fill: 'var(--surface-deep)', fillOpacity: 0.85 },
    labelBgPadding: [4, 2],
    labelBgBorderRadius: 4,
  }))
}

// ── Component ─────────────────────────────────────────────────────────────────

export function ArchitectureFlow({ services }: { services: Record<string, { status: string; url: string }> }) {
  const [open, setOpen] = useState(true)

  const getStatus = (key: string): SKey => {
    const s = services[key]?.status
    return s === 'ok' || s === 'degraded' || s === 'down' ? s : 'unknown'
  }

  const nodes: Node[] = useMemo(
    () => [
      ...GROUP_DEFS.map(g => ({
        id: g.id,
        type: 'groupLabel',
        position: { x: g.x, y: g.y },
        data: { label: g.label },
        draggable: false,
        selectable: false,
      })),
      ...NODE_DEFS.map(n => ({
        id: n.id,
        type: 'status',
        position: { x: n.x, y: n.y },
        data: { label: n.label, status: getStatus(n.id) },
      })),
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [services],
  )

  const edges = useMemo(() => buildEdges(), [])

  return (
    <div className="space-y-2">
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-2 text-xs font-semibold text-muted-foreground uppercase tracking-widest hover:text-foreground transition-colors"
      >
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        Platform Architecture
      </button>

      {open && (
        <div
          className="rounded-xl overflow-hidden"
          style={{ background: 'var(--surface-deep)', border: '1px solid var(--border-sm)' }}
        >
          <div style={{ height: 520 }}>
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              fitView
              fitViewOptions={{ padding: 0.12 }}
              nodesConnectable={false}
              edgesFocusable={false}
              nodesFocusable={false}
              minZoom={0.3}
              maxZoom={1.6}
            >
              <Background gap={20} size={1} color="var(--border-sm)" />
              <Controls showInteractive={false} position="bottom-right" />
            </ReactFlow>
          </div>

          {/* Legend */}
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 px-5 py-4 text-xs text-muted-foreground border-t" style={{ borderColor: 'var(--border-sm)' }}>
            {([['#10b981', 'Online'], ['#f59e0b', 'Degraded'], ['#ef4444', 'Offline']] as [string, string][]).map(([color, label]) => (
              <span key={label} className="flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full inline-block" style={{ background: color }} />
                {label}
              </span>
            ))}
            <span className="flex items-center gap-1.5">
              <span className="inline-block w-5" style={{ borderTop: '1.5px solid var(--muted-foreground)' }} />
              data flow
            </span>
            <span className="flex items-center gap-1.5">
              <span className="inline-block w-5" style={{ borderTop: '1.5px dashed var(--faint-text)' }} />
              observability / proxy
            </span>
            <span className="ml-auto opacity-60">drag nodes · scroll to zoom</span>
          </div>
        </div>
      )}
    </div>
  )
}
