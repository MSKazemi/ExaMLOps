import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { FeatureView } from '@/lib/features'
import { Features } from './Features'

const VIEWS: FeatureView[] = [
  {
    name: 'power_fv',
    entity: 'job',
    features: ['cpu', 'mem'],
    source: null,
    ttl_seconds: 3600,
    dataset_revision: null,
    updated_at: null,
  },
]

const apiFetch = vi.fn((path: string, opts?: { method?: string }) => {
  if (path === '/api/feature-store/views' && (!opts || opts.method !== 'POST')) return Promise.resolve(VIEWS)
  return Promise.resolve({ name: 'newfv', entity: 'job', features: ['a'], ttlSeconds: 0 })
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string, { method?: string }])),
  useMe: () => ({ data: undefined }),
}))

function renderFeatures() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Features />
    </QueryClientProvider>,
  )
}

describe('Features console (BL-022)', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('lists views read-only for viewers (no apply form)', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderFeatures()
    await waitFor(() => expect(screen.getByText('power_fv')).toBeInTheDocument())
    expect(screen.getByText('cpu, mem')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Apply view/i })).not.toBeInTheDocument()
  })

  it('lets an admin apply a view (comma-split features) through the endpoint', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderFeatures()
    fireEvent.change(await screen.findByLabelText('Feature view name'), { target: { value: 'newfv' } })
    fireEvent.change(screen.getByLabelText('Feature view entity'), { target: { value: 'job' } })
    fireEvent.change(screen.getByLabelText('Feature view features'), { target: { value: 'a, b , c' } })
    fireEvent.click(screen.getByRole('button', { name: /Apply view/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith('/api/feature-store/views', expect.objectContaining({ method: 'POST' })),
    )
    const call = apiFetch.mock.calls.find((c) => c[0] === '/api/feature-store/views' && (c[1] as { method?: string })?.method === 'POST')!
    expect(JSON.parse((call[1] as { body: string }).body)).toMatchObject({ name: 'newfv', entity: 'job', features: ['a', 'b', 'c'] })
  })

  it('client-validates required fields before calling', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderFeatures()
    fireEvent.click(await screen.findByRole('button', { name: /Apply view/i }))
    await waitFor(() => expect(screen.getByText(/at least one feature are required/)).toBeInTheDocument())
    expect(apiFetch.mock.calls.filter((c) => (c[1] as { method?: string })?.method === 'POST')).toHaveLength(0)
  })
})
