import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { AssetGraph } from '@/lib/assets'
import { Assets } from './Assets'

const GRAPH: AssetGraph = {
  counts: { total: 3, stale: 1, fresh: 2 },
  assets: [
    { name: 'dataset:FData', kind: 'dataset', description: null, version: 2, lastMaterializedAt: '2026-09-11',
      deps: [], dependents: ['jobs_features'], fresh: true, reasons: [], undeclaredDeps: [] },
    { name: 'jobs_features', kind: 'feature', description: 'feature view', version: 1, lastMaterializedAt: '2026-09-10',
      deps: ['dataset:FData'], dependents: ['jpcp'], fresh: false,
      reasons: ['upstream dataset:FData changed (1 → 2)'], undeclaredDeps: [] },
    { name: 'jpcp', kind: 'model', description: null, version: 3, lastMaterializedAt: '2026-09-10',
      deps: ['jobs_features'], dependents: [], fresh: true, reasons: [], undeclaredDeps: [] },
  ],
}

vi.mock('@/lib/api', () => ({
  apiFetch: () => Promise.resolve(GRAPH),
  useMe: () => ({ data: undefined }),
}))

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Assets />
    </QueryClientProvider>,
  )
}

describe('Assets page (ADR 0036 cl.5)', () => {
  it('draws the DAG with every node labelled by state, plus a data table', async () => {
    const { container } = renderPage()
    expect(await screen.findByRole('group', { name: /Asset dependency graph: 3 assets, 1 stale/ })).toBeInTheDocument()
    const stale = container.querySelector('[data-asset="jobs_features"]')
    expect(stale?.getAttribute('data-state')).toBe('stale')
    expect(within(stale as HTMLElement).getByText(/feature · stale/)).toBeInTheDocument()
    expect(screen.getByText('Data table')).toBeInTheDocument()
  })

  it('explains why an asset is stale when selected', async () => {
    const { container } = renderPage()
    await screen.findByRole('group', { name: /Asset dependency graph/ })
    fireEvent.click(container.querySelector('[data-asset="jobs_features"]') as Element)
    const detail = screen.getByRole('region', { name: 'Asset jobs_features' })
    expect(within(detail).getByText('upstream dataset:FData changed (1 → 2)')).toBeInTheDocument()
    expect(within(detail).getByText('dataset:FData')).toBeInTheDocument()
    expect(within(detail).getByLabelText('Stale')).toBeInTheDocument()
  })
})
