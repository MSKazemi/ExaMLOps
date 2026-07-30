import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ProviderRow } from '@/lib/providers'
import { Providers } from './Providers'

const ROW: ProviderRow = {
  domain: 'cost',
  name: 'c1',
  path: '/x/cost/c1.py',
  ok: true,
  error: null,
  active: false,
}

// `useMe` drives the capability gate in ConsoleView — swapped per test.
let me: { data: { capabilities: string[]; tenant: string } | undefined } = { data: undefined }

// 2-arg signature so `.mock.calls[1]` type-checks under `tsc -b` (production build includes tests).
const apiFetch = vi.fn((path: string, _opts?: { method?: string; body?: string }) => {
  if (path.startsWith('/api/v1/providers?project=')) return Promise.resolve([ROW])
  return Promise.resolve({ project: 'default', domain: 'cost', name: 'c1', active: true })
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string, { method?: string; body?: string }])),
  useMe: () => me,
}))

function renderProviders() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Providers />
    </QueryClientProvider>,
  )
}

describe('Providers console', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    me = { data: undefined }
    localStorage.clear()
  })

  it('lists a project’s providers and disables admin actions for viewers', async () => {
    me = { data: { capabilities: [], tenant: 'default' } }
    renderProviders()
    await waitFor(() => expect(screen.getByText('c1')).toBeInTheDocument())
    // The list query fired for the default project.
    expect(apiFetch).toHaveBeenCalledWith('/api/v1/providers?project=default')
    // Capability-gated actions are shown but disabled (never hidden) for a viewer.
    expect(screen.getByRole('button', { name: 'Activate' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'New provider' })).toBeDisabled()
  })

  it('activates a provider through the shared providers endpoint for an admin', async () => {
    me = { data: { capabilities: ['providers.manage'], tenant: 'default' } }
    renderProviders()
    const activate = await screen.findByRole('button', { name: 'Activate' })
    expect(activate).toBeEnabled()
    fireEvent.click(activate)
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith(
        '/api/v1/providers/default/cost/c1/activate',
        expect.objectContaining({ method: 'POST' }),
      ),
    )
    // The mutation is the second apiFetch call (first is the on-mount list).
    expect(apiFetch.mock.calls[1][0]).toBe('/api/v1/providers/default/cost/c1/activate')
    expect(apiFetch.mock.calls[1][1]).toMatchObject({ method: 'POST' })
  })
})
