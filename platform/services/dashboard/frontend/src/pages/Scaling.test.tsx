import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { AutoscaleView, RoutingView } from '@/lib/scaling'
import { Scaling } from './Scaling'

const AUTOSCALE: AutoscaleView = {
  model: 'JPCP',
  config: {
    model: 'JPCP',
    tenant: 'default',
    min_replicas: 0,
    max_replicas: 8,
    target_metric: 'queue_depth',
    target_value: 12,
    scale_to_zero_after_s: 300,
    warm_pool: 0,
    gpu_fraction: 1,
    enabled: 1,
    updated_at: null,
  },
  events: [
    { id: 1, model: 'JPCP', from_replicas: 2, to_replicas: 0, reason: 'idle scale-to-zero', metric_value: null, cold_start_s: null, ts: '2026-07-30T10:00:00Z' },
  ],
  savings: { model: 'JPCP', scale_to_zero_events: 1, saved_gpu_hours: 0.083, saved_cost: 0.166 },
}

const ROUTING: RoutingView = {
  model: 'JPCP',
  config: {
    model: 'JPCP',
    tenant: 'default',
    mode: 'cache_aware',
    slo_latency_ms: 500,
    disaggregate: 0,
    prefill_pool: null,
    decode_pool: null,
    updated_at: null,
  },
  stats: { total: 10, hits: 7, hit_rate: 0.7, by_decision: { cache_hit: 7, round_robin: 3 } },
}

const apiFetch = vi.fn((path: string, _opts?: { method?: string; body?: string }) => {
  if (path.startsWith('/api/v1/scaling/autoscale')) return Promise.resolve(AUTOSCALE)
  if (path.startsWith('/api/v1/scaling/routing')) return Promise.resolve(ROUTING)
  return Promise.resolve({})
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string])),
  useMe: () => ({ data: undefined }),
}))

function renderScaling() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Scaling />
    </QueryClientProvider>,
  )
}

describe('Scaling & Routing console', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('shows autoscale + routing config read-only for viewers (write controls disabled)', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderScaling()
    await waitFor(() => expect(screen.getByText(/GPU-h/)).toBeInTheDocument())
    // Cache-hit-rate stat from routing config is rendered.
    expect(screen.getByText(/70.0% · 10 events/)).toBeInTheDocument()
    // Write controls are present but disabled for viewers (disabled-with-reason, F15 R3).
    expect(screen.getByRole('button', { name: /Set policy/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /Set routing/i })).toBeDisabled()
    expect(screen.getByText(/Requires the admin role/i)).toBeInTheDocument()
  })

  it('lets an admin set an autoscale policy through the endpoint', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderScaling()
    fireEvent.change(await screen.findByLabelText('Min replicas'), { target: { value: '1' } })
    fireEvent.change(screen.getByLabelText('Max replicas'), { target: { value: '6' } })
    fireEvent.click(screen.getByRole('button', { name: /Set policy/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith('/api/v1/scaling/autoscale', expect.objectContaining({ method: 'POST' })),
    )
    const call = apiFetch.mock.calls.find(
      (c) => c[0] === '/api/v1/scaling/autoscale' && (c[1] as { method?: string })?.method === 'POST',
    )!
    expect(JSON.parse((call[1] as { body: string }).body)).toMatchObject({ model: 'JPCP', minReplicas: 1, maxReplicas: 6 })
  })

  it('lets an admin set a routing config through the endpoint', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderScaling()
    fireEvent.change(await screen.findByLabelText('Routing mode'), { target: { value: 'cache_aware' } })
    fireEvent.click(screen.getByRole('button', { name: /Set routing/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith('/api/v1/scaling/routing', expect.objectContaining({ method: 'POST' })),
    )
    const call = apiFetch.mock.calls.find(
      (c) => c[0] === '/api/v1/scaling/routing' && (c[1] as { method?: string })?.method === 'POST',
    )!
    expect(JSON.parse((call[1] as { body: string }).body)).toMatchObject({ model: 'JPCP', mode: 'cache_aware' })
  })

  it('client-validates the replica range before calling', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderScaling()
    fireEvent.change(await screen.findByLabelText('Min replicas'), { target: { value: '5' } })
    fireEvent.change(screen.getByLabelText('Max replicas'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: /Set policy/i }))
    await waitFor(() => expect(screen.getByText(/Require 0/)).toBeInTheDocument())
    expect(
      apiFetch.mock.calls.filter(
        (c) => c[0] === '/api/v1/scaling/autoscale' && (c[1] as { method?: string })?.method === 'POST',
      ),
    ).toHaveLength(0)
  })
})
