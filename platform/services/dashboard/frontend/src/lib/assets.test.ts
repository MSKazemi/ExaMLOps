import { describe, it, expect } from 'vitest'
import { layoutDag, assetState, type AssetNode } from './assets'

const node = (name: string, deps: string[] = [], over: Partial<AssetNode> = {}): AssetNode => ({
  name,
  kind: 'model',
  description: null,
  version: 1,
  lastMaterializedAt: '2026-09-11',
  deps,
  dependents: [],
  fresh: true,
  reasons: [],
  undeclaredDeps: [],
  ...over,
})

describe('layoutDag — asset DAG layout (ADR 0036 cl.5)', () => {
  it('places each asset one column right of its deepest upstream', () => {
    const l = layoutDag([node('ds'), node('feat', ['ds']), node('model', ['feat', 'ds'])])
    const col = Object.fromEntries(l.nodes.map((n) => [n.name, n.layer]))
    expect(col).toEqual({ ds: 0, feat: 1, model: 2 })
    for (const e of l.edges) {
      const from = l.nodes.find((n) => n.name === e.from)!
      const to = l.nodes.find((n) => n.name === e.to)!
      expect(to.x).toBeGreaterThan(from.x) // every edge points rightwards
    }
  })

  it('is deterministic: a column is sorted by name whatever the input order', () => {
    const a = layoutDag([node('b'), node('a'), node('c', ['a', 'b'])])
    const b = layoutDag([node('c', ['b', 'a']), node('b'), node('a')])
    expect(a.nodes.map((n) => [n.name, n.x, n.y])).toEqual(b.nodes.map((n) => [n.name, n.x, n.y]))
    expect(a.nodes.filter((n) => n.layer === 0).map((n) => n.name)).toEqual(['a', 'b'])
  })

  it('keeps an undeclared upstream as a visible placeholder', () => {
    const l = layoutDag([node('model', ['dataset:ghost'])])
    const ghost = l.nodes.find((n) => n.name === 'dataset:ghost')
    expect(ghost?.state).toBe('undeclared')
    expect(l.edges).toEqual([{ from: 'dataset:ghost', to: 'model' }])
  })

  it('reports a cycle instead of recursing forever', () => {
    const l = layoutDag([node('a', ['b']), node('b', ['a'])])
    expect(l.cycle.length).toBeGreaterThan(0)
    expect(l.nodes).toHaveLength(2)
  })

  it('sizes the canvas to the layout', () => {
    const l = layoutDag([node('x'), node('y'), node('z', ['x'])])
    expect(l.width).toBeGreaterThan(0)
    expect(l.height).toBeGreaterThan(0)
    expect(layoutDag([]).nodes).toEqual([])
  })
})

describe('assetState', () => {
  it('distinguishes fresh, stale, never built and undeclared', () => {
    expect(assetState(node('a'))).toBe('fresh')
    expect(assetState(node('a', [], { fresh: false, reasons: ['upstream ds changed (1 → 2)'] }))).toBe('stale')
    expect(assetState(node('a', [], { fresh: false, version: 0, reasons: ['never materialized'] }))).toBe('never')
    expect(assetState(undefined)).toBe('undeclared')
  })
})
