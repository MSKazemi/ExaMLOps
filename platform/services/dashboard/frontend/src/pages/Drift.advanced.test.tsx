import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AdvancedDriftTab } from './Drift'

const EVENTS = [
  { id: 3, ts: '2026-09-20 10:00:00', model: 'JPCP', drift_kind: 'concept', severity: 'CRITICAL', score: 4.25, metric: 'abs_error', detail: null },
  { id: 2, ts: '2026-09-20 09:00:00', model: 'JPCP', drift_kind: 'data_quality', severity: 'WARN', score: 0.3, metric: null, detail: null },
  { id: 1, ts: '2026-09-20 08:00:00', model: 'JPCP', drift_kind: 'prediction', severity: 'OK', score: null, metric: null, detail: null },
]

const apiFetch = vi.fn((_path: string) => Promise.resolve(EVENTS))
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
  it('shows concept and data-quality events only, with their severity and score', async () => {
    renderTab()
    expect(await screen.findByText('CRITICAL')).toBeInTheDocument()
    expect(screen.getByText('4.250')).toBeInTheDocument()
    expect(screen.getByText('data quality')).toBeInTheDocument()
    expect(screen.queryByText('prediction')).not.toBeInTheDocument()
    expect(apiFetch).toHaveBeenCalledWith('/api/drift/events')
  })
})
