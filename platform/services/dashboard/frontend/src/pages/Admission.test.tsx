import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { AdmissionView } from '@/lib/admission'
import { Admission } from './Admission'

const ADMISSION: AdmissionView = {
  stats: { queued: 3, running: 1, done: 5, rejected: 0, failed: 2 },
  total: 11,
}

const apiFetch = vi.fn((path: string, _opts?: { method?: string; body?: string }) => {
  if (path.startsWith('/api/v1/admission')) return Promise.resolve(ADMISSION)
  return Promise.resolve({})
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string])),
  useMe: () => ({ data: undefined }),
}))

function renderAdmission() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Admission />
    </QueryClientProvider>,
  )
}

describe('Admission console', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('shows queue depth read-only for viewers (submit control disabled)', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderAdmission()
    // Queued=3 stat rendered from the queue-depth response.
    await waitFor(() => expect(screen.getByText('3')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /Submit/i })).toBeDisabled()
    expect(screen.getByText(/Requires the admin role/i)).toBeInTheDocument()
  })

  it('lets an admin submit a work item through the endpoint', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderAdmission()
    fireEvent.change(await screen.findByLabelText('Work kind'), { target: { value: 'pipeline' } })
    fireEvent.change(screen.getByLabelText('Tenant'), { target: { value: 'acme' } })
    fireEvent.click(screen.getByRole('button', { name: /Submit/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith('/api/v1/admission', expect.objectContaining({ method: 'POST' })),
    )
    const call = apiFetch.mock.calls.find(
      (c) => c[0] === '/api/v1/admission' && (c[1] as { method?: string })?.method === 'POST',
    )!
    expect(JSON.parse((call[1] as { body: string }).body)).toMatchObject({ kind: 'pipeline', tenant: 'acme' })
  })

  it('client-validates the JSON payload before calling', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderAdmission()
    fireEvent.change(await screen.findByLabelText('Payload JSON'), { target: { value: '{bad' } })
    fireEvent.click(screen.getByRole('button', { name: /Submit/i }))
    await waitFor(() => expect(screen.getByText(/Payload must be valid JSON/)).toBeInTheDocument())
    expect(
      apiFetch.mock.calls.filter(
        (c) => c[0] === '/api/v1/admission' && (c[1] as { method?: string })?.method === 'POST',
      ),
    ).toHaveLength(0)
  })
})
