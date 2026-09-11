import { ChartFrame } from './ChartFrame'
import { NODE_H, NODE_W, layoutDag, type AssetNode, type AssetState } from '@/lib/assets'

// State → theme token + a text label (colour is never the only cue: every node prints its state).
const STATE: Record<AssetState, { color: string; label: string; dash?: string }> = {
  fresh: { color: 'var(--success-text)', label: 'fresh' },
  stale: { color: 'var(--warning-text)', label: 'stale' },
  never: { color: 'var(--error-text)', label: 'never built' },
  undeclared: { color: 'var(--subtle-text)', label: 'undeclared', dash: '4 3' },
}

export interface AssetDagProps {
  assets: AssetNode[]
  selected?: string | null
  onSelect?: (name: string) => void
}

/**
 * AssetDag — the software-defined asset graph (ADR 0036 clause 5), dependency-free SVG (F4).
 *
 * Left→right by dependency depth; each node shows its name, kind and freshness in text, and edges
 * run from an upstream to the asset built from it. Wrapped in ChartFrame, so the graph also exists
 * as a data table for screen readers and keyboard users.
 */
export function AssetDag({ assets, selected, onSelect }: AssetDagProps) {
  const layout = layoutDag(assets)
  const at = new Map(layout.nodes.map((n) => [n.name, n]))
  const pad = 8
  const table = {
    columns: [
      { key: 'asset', label: 'Asset' },
      { key: 'kind', label: 'Kind' },
      { key: 'state', label: 'State' },
      { key: 'version', label: 'Version' },
      { key: 'upstream', label: 'Upstream' },
    ],
    rows: layout.nodes.map((n) => {
      const a = assets.find((x) => x.name === n.name)
      return {
        asset: n.name,
        kind: n.kind,
        state: STATE[n.state].label,
        version: a?.version ?? '—',
        upstream: a?.deps.join(', ') || '—',
      }
    }),
  }
  return (
    <ChartFrame
      ariaLabel={`Asset dependency graph: ${assets.length} assets, ${
        assets.filter((a) => !a.fresh).length
      } stale or never built`}
      table={table}
    >
      <div className="overflow-x-auto">
        <svg
          width={layout.width + pad * 2}
          height={layout.height + pad * 2}
          viewBox={`${-pad} ${-pad} ${layout.width + pad * 2} ${layout.height + pad * 2}`}
        >
          {layout.edges.map((e) => {
            const a = at.get(e.from)
            const b = at.get(e.to)
            if (!a || !b) return null
            const x1 = a.x + NODE_W
            const y1 = a.y + NODE_H / 2
            const x2 = b.x
            const y2 = b.y + NODE_H / 2
            const mx = (x1 + x2) / 2
            return (
              <path
                key={`${e.from}->${e.to}`}
                d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
                fill="none"
                stroke="var(--border)"
                strokeWidth={1.5}
              />
            )
          })}
          {layout.nodes.map((n) => {
            const s = STATE[n.state]
            const isSel = selected === n.name
            return (
              <g
                key={n.name}
                transform={`translate(${n.x},${n.y})`}
                onClick={() => onSelect?.(n.name)}
                style={{ cursor: onSelect ? 'pointer' : 'default' }}
                data-asset={n.name}
                data-state={n.state}
              >
                <rect
                  width={NODE_W}
                  height={NODE_H}
                  rx={8}
                  fill="var(--card)"
                  stroke={s.color}
                  strokeWidth={isSel ? 2.5 : 1.5}
                  strokeDasharray={s.dash}
                />
                <text x={10} y={18} fontSize={12} fontWeight={600} fill="var(--foreground)">
                  {n.name.length > 22 ? `${n.name.slice(0, 21)}…` : n.name}
                </text>
                <text x={10} y={34} fontSize={10} fill={s.color}>
                  {n.kind} · {s.label}
                </text>
              </g>
            )
          })}
        </svg>
      </div>
    </ChartFrame>
  )
}
