import { describe, it, expect } from 'vitest'
import {
  applySort,
  applyFilter,
  computeFacets,
  queryRows,
  toggleSelection,
  toCsv,
  encodeQuery,
  decodeQuery,
  type Column,
} from './datagrid'

interface Row {
  id: string
  model: string
  env: string
  cost: number
}

const COLUMNS: Column<Row>[] = [
  { key: 'model', header: 'Model', accessor: (r) => r.model, sortable: true },
  { key: 'env', header: 'Env', accessor: (r) => r.env, facet: true },
  { key: 'cost', header: 'Cost', accessor: (r) => r.cost, sortable: true },
]

const ROWS: Row[] = [
  { id: '1', model: 'jpcp', env: 'prod', cost: 30 },
  { id: '2', model: 'awgn', env: 'staging', cost: 10 },
  { id: '3', model: 'jpcp', env: 'staging', cost: 20 },
]

describe('applySort', () => {
  it('sorts ascending by a numeric column', () => {
    const out = applySort(ROWS, COLUMNS, [{ col: 'cost', dir: 'asc' }])
    expect(out.map((r) => r.cost)).toEqual([10, 20, 30])
  })
  it('sorts descending and is stable within ties', () => {
    const out = applySort(ROWS, COLUMNS, [{ col: 'model', dir: 'asc' }])
    // two jpcp rows keep their original relative order (id 1 before id 3)
    const jpcp = out.filter((r) => r.model === 'jpcp')
    expect(jpcp.map((r) => r.id)).toEqual(['1', '3'])
  })
  it('returns the input unchanged when no sort is given', () => {
    expect(applySort(ROWS, COLUMNS, undefined)).toBe(ROWS)
  })
})

describe('applyFilter', () => {
  it('substring-matches case-insensitively', () => {
    expect(applyFilter(ROWS, COLUMNS, { model: 'JP' }).length).toBe(2)
  })
  it('treats empty filter values as no constraint', () => {
    expect(applyFilter(ROWS, COLUMNS, { model: '' }).length).toBe(3)
  })
})

describe('computeFacets', () => {
  it('counts values only for facet columns', () => {
    const facets = computeFacets(ROWS, COLUMNS)
    expect(facets).toEqual({ env: { prod: 1, staging: 2 } })
  })
})

describe('queryRows', () => {
  it('filters, sorts, and paginates with a stable total', () => {
    const res = queryRows(ROWS, COLUMNS, {
      filters: { model: 'jpcp' },
      sort: [{ col: 'cost', dir: 'desc' }],
      page: 0,
      pageSize: 1,
    })
    expect(res.total).toBe(2)
    expect(res.pageCount).toBe(2)
    expect(res.rows.map((r) => r.cost)).toEqual([30])
    expect(res.facets.env).toEqual({ prod: 1, staging: 1 })
  })
})

describe('toggleSelection', () => {
  it('adds then removes an id without mutating the input', () => {
    const a = new Set<string>()
    const b = toggleSelection(a, '1')
    expect(a.size).toBe(0)
    expect(b.has('1')).toBe(true)
    expect(toggleSelection(b, '1').has('1')).toBe(false)
  })
})

describe('toCsv', () => {
  it('emits a header row and escapes commas/quotes', () => {
    const csv = toCsv([{ id: 'x', model: 'a,b', env: 'p"q', cost: 1 }], COLUMNS)
    const [header, body] = csv.split('\n')
    expect(header).toBe('Model,Env,Cost')
    expect(body).toBe('"a,b","p""q",1')
  })
})

describe('encodeQuery / decodeQuery', () => {
  it('round-trips sort + filters + page', () => {
    const q = { sort: [{ col: 'cost', dir: 'desc' as const }], filters: { model: 'jp' }, page: 2 }
    const decoded = decodeQuery(encodeQuery(q))
    expect(decoded.sort).toEqual([{ col: 'cost', dir: 'desc' }])
    expect(decoded.filters).toEqual({ model: 'jp' })
    expect(decoded.page).toBe(2)
  })
  it('omits absent parts', () => {
    expect(decodeQuery(encodeQuery({}))).toEqual({ sort: undefined, filters: undefined, page: undefined })
  })
})
