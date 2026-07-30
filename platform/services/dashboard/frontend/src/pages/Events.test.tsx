import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { EventsView } from '@/lib/events'
import { Events } from './Events'

const EVENTS: EventsView = {
  stats: { pending: 3, published: 5, poison: 1 },
  total: 9,
}

const apiFetch = vi.fn((path: string, _opts?: { method?: string; body?: string }) => {
  if (path.startsWith('/api/v1/events')) return Promise.resolve(EVENTS)
  return Promise.resolve({})
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string])),
  useMe: () => ({ data: undefined }),
}))

function renderEvents() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Events />
    </QueryClientProvider>,
  )
}

describe('Events console', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('shows outbox backlog read-only for viewers (publish control disabled)', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderEvents()
    // Pending=3 stat rendered from the backlog response.
    await waitFor(() => expect(screen.getByText('3')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /Publish/i })).toBeDisabled()
    expect(screen.getByText(/Requires the admin role/i)).toBeInTheDocument()
  })

  it('lets an admin publish an event through the endpoint', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderEvents()
    fireEvent.change(await screen.findByLabelText('Event topic'), { target: { value: 'retrain.requested' } })
    fireEvent.click(screen.getByRole('button', { name: /Publish/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith('/api/v1/events', expect.objectContaining({ method: 'POST' })),
    )
    const call = apiFetch.mock.calls.find(
      (c) => c[0] === '/api/v1/events' && (c[1] as { method?: string })?.method === 'POST',
    )!
    expect(JSON.parse((call[1] as { body: string }).body)).toMatchObject({ topic: 'retrain.requested' })
  })

  it('client-validates the JSON payload before calling', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderEvents()
    fireEvent.change(await screen.findByLabelText('Payload JSON'), { target: { value: '{bad' } })
    fireEvent.click(screen.getByRole('button', { name: /Publish/i }))
    await waitFor(() => expect(screen.getByText(/Payload must be valid JSON/)).toBeInTheDocument())
    expect(
      apiFetch.mock.calls.filter(
        (c) => c[0] === '/api/v1/events' && (c[1] as { method?: string })?.method === 'POST',
      ),
    ).toHaveLength(0)
  })
})
