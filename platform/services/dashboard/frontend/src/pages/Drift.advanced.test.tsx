import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AdvancedDriftTab } from './Drift'

const EVENTS = [
  { id: 4, ts: '2026-09-20 11:00:00', model: 'CLF', drift_kind: 'concept', severity: 'CRITICAL', score: 0.3, metric: 'accuracy', detail: { label_free: true, confirmed_by_labels: true } },
  { id: 3, ts: '2026-09-20 10:00:00', model: 'JPCP', drift_kind: 'concept', severity: 'CRITICAL', score: 4.25, metric: 'abs_error', detail: null },
  { id: 2, ts: '2026-09-20 09:00:00', model: 'JPCP', drift_kind: 'data_quality', severity: 'WARN', score: 0.3, metric: null, detail: null },
  { id: 1, ts: '2026-09-20 08:00:00', model: 'JPCP', drift_kind: 'prediction', severity: 'OK', score: null, metric: null, detail: null },
]

const ESTIMATES = [
  { id: 2, ts: '2026-09-20 11:00:00', model: 'CLF', metric: 'accuracy', estimated: 0.62, realized: 0.55, baseline: 0.95, method: 'nannyml-cbpe', gap: 0.07 },
  { id: 1, ts: '2026-09-20 10:00:00', model: 'CLF', metric: 'accuracy', estimated: 0.91, realized: null, baseline: 0.95, method: 'cbpe-like', gap: null },
]

let routes: Record<string, unknown> = {}
const apiFetch = vi.fn((path: string) => {
  const key = Object.keys(routes).find((p) => path.startsWith(p))
  return key ? Promise.resolve(routes[key]) : Promise.reject(new Error(`unmocked ${path}`))
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string])),
  useMe: () => ({ data: undefined }),
}))

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AdvancedDriftTab />
    </QueryClientProvider>,
  )
}

describe('Advanced drift tab', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    routes = { '/api/drift/events': EVENTS, '/api/drift/perf-estimates': ESTIMATES }
  })

  it('shows concept and data-quality events only, with their severity and score', async () => {
    renderTab()
    expect(await screen.findByText('4.250')).toBeInTheDocument()
    expect(screen.getAllByText('CRITICAL').length).toBe(2)
    expect(screen.getByText('data quality')).toBeInTheDocument()
    expect(screen.queryByText('prediction')).not.toBeInTheDocument()
    expect(apiFetch).toHaveBeenCalledWith('/api/drift/events')
  })

  it('names a label-confirmed estimate for what it is', async () => {
    renderTab()
    expect(await screen.findByText('estimate (confirmed)')).toBeInTheDocument()
  })

  it('shows estimated vs realized performance, with the gap and awaiting-labels rows', async () => {
    renderTab()
    const table = await screen.findByRole('table', { name: 'Estimated vs realized performance' })
    expect(table).toHaveTextContent('0.620')
    expect(table).toHaveTextContent('0.550')
    expect(table).toHaveTextContent('0.070')
    expect(table).toHaveTextContent('nannyml-cbpe')
    expect(table).toHaveTextContent('awaiting labels')
    expect(apiFetch).toHaveBeenCalledWith('/api/drift/perf-estimates')
  })

  it('says so when the estimate endpoint fails instead of showing an empty panel', async () => {
    routes = { '/api/drift/events': EVENTS }
    renderTab()
    expect(await screen.findByText("Couldn't load performance estimates")).toBeInTheDocument()
  })

  it('explains an empty estimate log', async () => {
    routes = { '/api/drift/events': EVENTS, '/api/drift/perf-estimates': [] }
    renderTab()
    expect(await screen.findByText('No performance estimates yet')).toBeInTheDocument()
  })
})
