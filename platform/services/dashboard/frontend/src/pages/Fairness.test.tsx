import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { FairnessConfig } from '@/lib/fairness'
import { Fairness } from './Fairness'

const CONFIGS: FairnessConfig[] = [
  {
    model: 'JPCP',
    tenant: 'default',
    slice_attrs: ['region', 'cluster'],
    threshold: 0.15,
    min_samples: 30,
    gate_promotion: true,
    enabled: true,
    updated_at: null,
  },
]

const apiFetch = vi.fn((path: string, opts?: { method?: string }) => {
  if (path === '/api/fairness' && (!opts || opts.method !== 'POST')) return Promise.resolve(CONFIGS)
  return Promise.resolve({ model: 'MACK', sliceAttrs: ['region'], threshold: 0.1, gatePromotion: false, enabled: true })
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string, { method?: string }])),
  useMe: () => ({ data: undefined }),
}))

function renderFairness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Fairness />
    </QueryClientProvider>,
  )
}

describe('Fairness console (BL-023)', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('lists configs read-only for viewers (no configure form)', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderFairness()
    await waitFor(() => expect(screen.getByText('JPCP')).toBeInTheDocument())
    expect(screen.getByText('region, cluster')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Save config/i })).not.toBeInTheDocument()
  })

  it('lets an admin configure fairness (comma-split slice attrs) through the endpoint', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderFairness()
    fireEvent.change(await screen.findByLabelText('Fairness model'), { target: { value: 'MACK' } })
    fireEvent.change(screen.getByLabelText('Fairness slice attributes'), { target: { value: 'region, node ' } })
    fireEvent.click(screen.getByRole('button', { name: /Save config/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith('/api/fairness', expect.objectContaining({ method: 'POST' })),
    )
    const call = apiFetch.mock.calls.find((c) => c[0] === '/api/fairness' && (c[1] as { method?: string })?.method === 'POST')!
    expect(JSON.parse((call[1] as { body: string }).body)).toMatchObject({ model: 'MACK', sliceAttrs: ['region', 'node'] })
  })

  it('client-validates required slice attrs before calling', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderFairness()
    fireEvent.change(await screen.findByLabelText('Fairness model'), { target: { value: 'MACK' } })
    fireEvent.click(screen.getByRole('button', { name: /Save config/i }))
    await waitFor(() => expect(screen.getByText(/slice attribute are required/)).toBeInTheDocument())
    expect(apiFetch.mock.calls.filter((c) => (c[1] as { method?: string })?.method === 'POST')).toHaveLength(0)
  })
})
