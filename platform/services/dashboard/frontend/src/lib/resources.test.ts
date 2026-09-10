import { describe, expect, it } from 'vitest'
import type { CliCommand } from './cli'
import { groupResources, lockedParams, prefillFromRow, rowValue, rowsOf, runError, visibleColumns, type CliResource } from './resources'

const resource = (over: Partial<CliResource> = {}): CliResource => ({
  id: 'connection',
  title: 'Connections',
  description: '',
  panel: 'Projects & Workspaces',
  list: 'connection list',
  key: 'name',
  rows_key: null,
  create: 'connection create',
  show: 'connection show',
  edit: [],
  delete: 'connection delete',
  actions: [],
  bindings: { 'connection delete': { name: 'name' }, 'connection show': { name: 'name' } },
  columns: ['name', 'project', 'kind'],
  tiers: { 'connection list': 'read', 'connection delete': 'destructive' },
  ...over,
})

const del: CliCommand = {
  path: 'connection delete',
  group: 'connection',
  panel: 'Projects & Workspaces',
  help: '',
  short_help: '',
  examples: [],
  tier: 'destructive',
  reason: null,
  forced_args: [],
  params: [
    { name: 'name', kind: 'argument', type: 'string', required: true },
    { name: 'project', kind: 'option', type: 'string', opts: ['--project'] },
    { name: 'config', kind: 'option', type: 'string', opts: ['--config'] },
    { name: 'yes', kind: 'option', type: 'bool', flag: true, opts: ['--yes'], implied: true },
  ],
}

describe('rowsOf (mirrors examlops.cli.resources.rows_of)', () => {
  it('normalises every list shape the CLI prints', () => {
    const r = { key: 'name', rows_key: null }
    expect(rowsOf([{ name: 'a' }], r)).toEqual([{ name: 'a' }])
    expect(rowsOf(['a', 'b'], r)).toEqual([{ name: 'a' }, { name: 'b' }])
    expect(rowsOf({ path: '/x', policies: [{ name: 'p' }], error: null }, r)).toEqual([{ name: 'p' }])
    expect(rowsOf({ snapshots: [1], bundles: [2] }, { key: 'path', rows_key: 'bundles' })).toEqual([{ path: 2 }])
    expect(rowsOf({ ok: true, message: 'none' }, r)).toEqual([])
    expect(rowsOf(null, r)).toEqual([])
  })
})

describe('row binding (mirrors prefill)', () => {
  it('fills the bound key, then params named like scalar row fields', () => {
    const row = { name: 'c1', project: 'p1', config: { a: 1 }, kind: 'uri' }
    expect(prefillFromRow(resource(), del, row)).toEqual({ name: 'c1', project: 'p1' })
  })

  it('never puts a display string in a number field (mirrors fits)', () => {
    const setBudget: CliCommand = {
      ...del,
      path: 'finops budget set',
      params: [
        { name: 'project', kind: 'argument', type: 'string' },
        { name: 'cost', kind: 'option', type: 'float', opts: ['--cost'] },
        { name: 'gpu_hours', kind: 'option', type: 'float', opts: ['--gpu-hours'] },
        { name: 'period', kind: 'option', type: 'choice', opts: ['--period'], choices: ['month', 'year'] },
      ],
    }
    const r = resource({ bindings: { 'finops budget set': { project: 'project' } } })
    const row = { project: 'p1', cost: '0 / —', gpu_hours: 12.5, period: 'decade' }
    expect(prefillFromRow(r, setBudget, row)).toEqual({ project: 'p1', gpu_hours: '12.5' })
  })

  it('looks fields up case-insensitively', () => {
    expect(rowValue({ Name: 'jpcp' }, 'name')).toBe('jpcp')
    expect(rowValue({ 'key-hash': 'k' }, 'key_hash')).toBe('k')
  })

  it('locks exactly the identity params', () => {
    expect(lockedParams(resource(), del)).toEqual(['name'])
    expect(lockedParams(resource(), { ...del, path: 'connection test' })).toEqual([])
  })
})

describe('table columns', () => {
  it('puts declared columns first and caps the rest', () => {
    const rows = [{ created_at: 't', kind: 'uri', name: 'c1', project: 'p1', a: 1, b: 2, c: 3, d: 4, e: 5 }]
    const cols = visibleColumns(resource(), rows, 6)
    expect(cols.slice(0, 3)).toEqual(['name', 'project', 'kind'])
    expect(cols).toHaveLength(6)
  })

  it('shows the key column even when it is not declared', () => {
    expect(visibleColumns(resource({ columns: [] }), [{ x: 1, name: 'n' }])[0]).toBe('name')
  })
})

describe('grouping and errors', () => {
  it('groups by the CLI panel order', () => {
    const a = resource({ id: 'a', panel: 'Serving & Inference' })
    const b = resource({ id: 'b', panel: 'Models & Registry' })
    expect(groupResources([a, b], ['Models & Registry', 'Serving & Inference']).map(([p]) => p)).toEqual([
      'Models & Registry',
      'Serving & Inference',
    ])
  })

  it('explains a failed run with the CLI’s own error and hint', () => {
    expect(runError({ parsed: { error: 'No cluster given', hint: 'pass --cluster' }, stderr: '', error: null, status: 'failed' })).toBe(
      'No cluster given — pass --cluster',
    )
    expect(runError({ parsed: null, stderr: 'boom\nlast line\n', error: null, status: 'failed' })).toBe('last line')
  })
})
