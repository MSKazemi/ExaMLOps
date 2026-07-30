import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { AutopilotStatus } from '@/lib/autopilot'
import { Autopilot } from './Autopilot'

let status: AutopilotStatus = {
  enabled: false,
  envOverride: null,
  effective: false,
  recentRuns: [],
}

const apiFetch = vi.fn((path: string, _opts?: { method?: string; body?: string }) => {
  if (path === '/api/autopilot/status') return Promise.resolve(status)
  return Promise.resolve({ enabled: path.endsWith('/enable') })
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string, { method?: string }])),
  useMe: () => ({ data: undefined }),
}))

function renderAutopilot() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Autopilot />
    </QueryClientProvider>,
  )
}

describe('Autopilot console — kill-switch (BL-019)', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
    status = { enabled: false, envOverride: null, effective: false, recentRuns: [] }
  })
  afterEach(() => clearAuth())

  it('shows disabled status and no toggle for viewers', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderAutopilot()
    await waitFor(() => expect(screen.getByText('Disabled')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /Enable autopilot/i })).not.toBeInTheDocument()
    expect(screen.getByText('Admin only')).toBeInTheDocument()
  })

  it('lets an admin enable the kill-switch', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderAutopilot()
    fireEvent.click(await screen.findByRole('button', { name: /Enable autopilot/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith('/api/autopilot/enable', expect.objectContaining({ method: 'POST' })),
    )
  })

  it('surfaces an env override', async () => {
    status = { enabled: false, envOverride: true, effective: true, recentRuns: [] }
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderAutopilot()
    await waitFor(() => expect(screen.getByText(/EXAMLOPS_AUTOPILOT_ENABLED/)).toBeInTheDocument())
  })
})
