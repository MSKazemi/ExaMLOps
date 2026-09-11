import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './api'

// Software-defined asset views (ADR 0036 clause 5). Read-only: the DAG and each asset's freshness
// come from the dashboard's assets router, which calls the same examlops.assets.asset_status the
// `exa assets status` CLI does.

export interface AssetNode {
  name: string
  kind: string
  description: string | null
  version: number
  lastMaterializedAt: string | null
  deps: string[]
  dependents: string[]
  fresh: boolean
  reasons: string[]
  undeclaredDeps: string[]
}

export interface AssetGraph {
  assets: AssetNode[]
  counts: { total: number; stale: number; fresh: number }
}

export const useAssetGraph = () =>
  useQuery<AssetGraph>({
    queryKey: ['assets', 'graph'],
    queryFn: () => apiFetch<AssetGraph>('/api/assets'),
  })

/** How an asset reads on the page: fresh, stale, never built, or an undeclared placeholder. */
export type AssetState = 'fresh' | 'stale' | 'never' | 'undeclared'

export function assetState(a: AssetNode | undefined): AssetState {
  if (!a) return 'undeclared'
  if (a.fresh) return 'fresh'
  return a.version === 0 || a.reasons.includes('never materialized') ? 'never' : 'stale'
}

export interface PlacedNode {
  name: string
  layer: number
  row: number
  x: number
  y: number
  state: AssetState
  kind: string
}

export interface PlacedEdge {
  from: string
  to: string
}

export interface DagLayout {
  nodes: PlacedNode[]
  edges: PlacedEdge[]
  width: number
  height: number
  /** Names on a dependency cycle — an asset DAG must not have one; shown, never looped on. */
  cycle: string[]
}

export const NODE_W = 172
export const NODE_H = 44
const GAP_X = 64
const GAP_Y = 16

/**
 * Lay the DAG out left→right: an asset's column is the length of the longest dependency path
 * that reaches it (sources in column 0), so every edge points rightwards; within a column assets
 * are sorted by name, so the layout is deterministic. Pure — no DOM, unit-tested.
 *
 * An upstream that is named but never declared becomes a placeholder node (`undeclared`) rather
 * than vanishing, because a DAG that silently drops a missing input looks complete when it isn't.
 * A cycle (which `exa assets` should never allow) is detected and reported, not recursed into.
 */
export function layoutDag(assets: AssetNode[]): DagLayout {
  const byName = new Map(assets.map((a) => [a.name, a]))
  const names = new Set<string>(assets.map((a) => a.name))
  for (const a of assets) for (const d of a.deps) names.add(d)

  const depth = new Map<string, number>()
  const visiting = new Set<string>()
  const cycle = new Set<string>()
  const depthOf = (n: string): number => {
    const known = depth.get(n)
    if (known !== undefined) return known
    if (visiting.has(n)) {
      cycle.add(n)
      return 0
    }
    visiting.add(n)
    const deps = byName.get(n)?.deps ?? []
    const d = deps.length ? 1 + Math.max(...deps.map(depthOf)) : 0
    visiting.delete(n)
    depth.set(n, d)
    return d
  }
  for (const n of names) depthOf(n)

  const layers = new Map<number, string[]>()
  for (const n of [...names].sort()) {
    const l = depth.get(n) ?? 0
    layers.set(l, [...(layers.get(l) ?? []), n])
  }
  const nodes: PlacedNode[] = []
  let maxRows = 0
  for (const [layer, members] of [...layers.entries()].sort((a, b) => a[0] - b[0])) {
    maxRows = Math.max(maxRows, members.length)
    members.forEach((name, row) => {
      const a = byName.get(name)
      nodes.push({
        name,
        layer,
        row,
        x: layer * (NODE_W + GAP_X),
        y: row * (NODE_H + GAP_Y),
        state: assetState(a),
        kind: a?.kind ?? 'undeclared',
      })
    })
  }
  const edges: PlacedEdge[] = assets.flatMap((a) => a.deps.map((d) => ({ from: d, to: a.name })))
  const layerCount = layers.size || 1
  return {
    nodes,
    edges,
    width: layerCount * NODE_W + (layerCount - 1) * GAP_X,
    height: Math.max(1, maxRows) * NODE_H + Math.max(0, maxRows - 1) * GAP_Y,
    cycle: [...cycle].sort(),
  }
}
