import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { AbView, ShadowView } from '@/lib/traffic'
import { Traffic } from './Traffic'

const AB: AbView = {
  tests: [
    {
      id: 1,
      model: 'JPCP',
      name: 'exp-1',
      variant_a: 'Production',
      variant_b: 'Canary',
      split_pct: 70,
      status: 'running',
      started_at: '2026-07-30T10:00:00',
      ended_at: null,
      created_by: 'admin',
    },
  ],
  analysis: {
    model: 'JPCP',
    test_id: 1,
    variant_a: 'Production',
    variant_b: 'Canary',
    verdict: 'insufficient_sample',
    significant: false,
    winner: null,
    n_a: 2,
    n_b: 1,
    min_sample: 30,
  },
}

const SHADOW: ShadowView = {
  config: [
    {
      model: 'JPCP',
      shadow_alias: 'Staging',
      enabled: 1,
      updated_at: '2026-07-30T10:00:00',
      updated_by: 'admin',
    },
  ],
  results: [],
}

const apiFetch = vi.fn((path: string, _opts?: { method?: string; body?: string }) => {
  if (path.startsWith('/api/v1/traffic/ab/start'))
    return Promise.resolve(AB.tests[0])
  if (path.startsWith('/api/v1/traffic/ab/stop'))
    return Promise.resolve({ model: 'JPCP', stopped: true })
  if (path.startsWith('/api/v1/traffic/ab')) return Promise.resolve(AB)
  if (path.startsWith('/api/v1/traffic/shadow')) return Promise.resolve(SHADOW)
  return Promise.resolve({})
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string, { method?: string; body?: string }?])),
  useMe: () => ({ data: undefined }),
}))

function renderTraffic() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Traffic />
    </QueryClientProvider>,
  )
}

describe('Traffic console', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('shows A/B tests read-only for viewers (start control disabled)', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderTraffic()
    await waitFor(() => expect(screen.getByText('exp-1')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /Start test/i })).toBeDisabled()
    expect(screen.getByText(/Requires the admin role/i)).toBeInTheDocument()
  })

  it('lets an admin start an A/B test through the endpoint', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderTraffic()
    await waitFor(() => expect(screen.getByText('exp-1')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /Start test/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith(
        '/api/v1/traffic/ab/start',
        expect.objectContaining({ method: 'POST' }),
      ),
    )
    const call = apiFetch.mock.calls.find((c) => c[0] === '/api/v1/traffic/ab/start')!
    expect(JSON.parse((call[1] as { body: string }).body)).toMatchObject({
      model: 'JPCP',
      split: 50,
    })
  })

  it('lets an admin enable shadow through the endpoint', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderTraffic()
    await waitFor(() => expect(screen.getByText('exp-1')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /^Enable$/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith(
        '/api/v1/traffic/shadow',
        expect.objectContaining({ method: 'POST' }),
      ),
    )
    const call = apiFetch.mock.calls.find(
      (c) => c[0] === '/api/v1/traffic/shadow' && (c[1] as { method?: string })?.method === 'POST',
    )!
    expect(JSON.parse((call[1] as { body: string }).body)).toMatchObject({
      model: 'JPCP',
      enabled: true,
    })
  })

  it('client-validates the split before calling start', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderTraffic()
    await waitFor(() => expect(screen.getByText('exp-1')).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Split percent'), { target: { value: '150' } })
    fireEvent.click(screen.getByRole('button', { name: /Start test/i }))
    await waitFor(() =>
      expect(screen.getByText(/Split must be an integer between 0 and 100/)).toBeInTheDocument(),
    )
    expect(apiFetch.mock.calls.filter((c) => c[0] === '/api/v1/traffic/ab/start')).toHaveLength(0)
  })
})
