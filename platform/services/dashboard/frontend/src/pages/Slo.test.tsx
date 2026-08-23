import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { SloSpec } from '@/lib/slo'
import { Slo } from './Slo'

const SPECS: SloSpec[] = [
  {
    model: 'JPCP',
    tenant: 'default',
    name: 'availability',
    sli_source: 'prometheus',
    sli_query: null,
    target: 0.99,
    window: '30d',
    higher_is_better: true,
    version: 1,
    gate_promotion: true,
    updated_at: null,
    status: { sli: 0.995, budgetRemaining: 0.5, burnRate: 0.5, ok: true, n: 100, measured: true },
  },
]

const apiFetch = vi.fn((path: string, _opts?: { method?: string; body?: string }) => {
  if (path === '/api/slo') return Promise.resolve(SPECS)
  return Promise.resolve({ model: 'MACK', name: 'latency', target: 0.98, gatePromotion: false })
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string])),
  useMe: () => ({ data: undefined }),
}))

function renderSlo() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Slo />
    </QueryClientProvider>,
  )
}

describe('SLOs console (BL-020)', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('shows specs + live status read-only for viewers (no define form)', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderSlo()
    await waitFor(() => expect(screen.getByText('availability')).toBeInTheDocument())
    expect(screen.getByText('Meeting')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Set SLO/i })).not.toBeInTheDocument()
  })

  it('lets an admin define an SLO through the endpoint', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderSlo()
    fireEvent.change(await screen.findByLabelText('SLO model'), { target: { value: 'MACK' } })
    fireEvent.change(screen.getByLabelText('SLO name'), { target: { value: 'latency' } })
    fireEvent.change(screen.getByLabelText('SLO target'), { target: { value: '0.98' } })
    fireEvent.click(screen.getByRole('button', { name: /Set SLO/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith('/api/slo', expect.objectContaining({ method: 'POST' })),
    )
    const call = apiFetch.mock.calls.find((c) => c[0] === '/api/slo' && (c[1] as { method?: string })?.method === 'POST')!
    expect(JSON.parse((call[1] as { body: string }).body)).toMatchObject({ model: 'MACK', name: 'latency', target: 0.98 })
  })

  it('client-validates the target range before calling', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderSlo()
    fireEvent.change(await screen.findByLabelText('SLO model'), { target: { value: 'MACK' } })
    fireEvent.change(screen.getByLabelText('SLO target'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: /Set SLO/i }))
    await waitFor(() => expect(screen.getByText(/Target must be in/)).toBeInTheDocument())
    expect(apiFetch.mock.calls.filter((c) => (c[1] as { method?: string })?.method === 'POST')).toHaveLength(0)
  })
})

describe('an SLO nobody has measured', () => {
  it('is not rendered as one meeting its target', async () => {
    // Zero samples score a perfect SLI upstream, so this row used to show a green "Meeting"
    // pill reading "SLI 100.00% · budget 100%" — a target nobody had measured, published as met.
    apiFetch.mockImplementationOnce(() =>
      Promise.resolve([
        {
          ...SPECS[0],
          status: { sli: 1, budgetRemaining: 1, burnRate: 0, ok: null, n: 0, measured: false },
        },
      ]),
    )
    renderSlo()
    expect(await screen.findByText('Unmeasured')).toBeTruthy()
    expect(screen.queryByText('Meeting')).toBeNull()
    expect(screen.getByText('no samples recorded')).toBeTruthy()
  })
})
