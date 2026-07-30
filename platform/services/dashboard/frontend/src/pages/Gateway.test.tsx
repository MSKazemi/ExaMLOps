import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { VirtualKey } from '@/lib/gateway'
import { Gateway } from './Gateway'

const KEY: VirtualKey = {
  key_hash: 'abc123def456aaaaaaaa',
  tenant: 'default',
  project: 'research',
  models: ['JPCP'],
  budget_usd: 25,
  spent_usd: 1.5,
  created_by: 'alice',
  created_at: null,
  revoked: false,
}

const apiFetch = vi.fn((path: string, opts?: { method?: string }) => {
  if (path === '/api/gateway/keys' && (!opts || opts.method !== 'POST')) return Promise.resolve([KEY])
  if (path === '/api/gateway/keys' && opts?.method === 'POST')
    return Promise.resolve({ key: 'exa-RAWSECRET123', tenant: 'default', project: 'research', models: [], budgetUsd: null })
  return Promise.resolve({ keyHash: KEY.key_hash, revoked: true })
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string, { method?: string }])),
  useMe: () => ({ data: undefined }),
}))

function renderGateway() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Gateway />
    </QueryClientProvider>,
  )
}

describe('Gateway console — virtual keys (BL-017)', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('shows the key hash (not a raw key) and hides admin actions from viewers', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderGateway()
    await waitFor(() => expect(screen.getByText(/abc123def456/)).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /Issue key/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Revoke key/i })).not.toBeInTheDocument()
  })

  it('issues a key and surfaces the raw value once', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderGateway()
    fireEvent.click(await screen.findByRole('button', { name: /Issue key/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith('/api/gateway/keys', expect.objectContaining({ method: 'POST' })),
    )
    expect(await screen.findByLabelText('New virtual key')).toHaveTextContent('exa-RAWSECRET123')
  })

  it('revokes a key through the revoke endpoint', async () => {
    vi.stubGlobal('confirm', () => true)
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderGateway()
    fireEvent.click(await screen.findByRole('button', { name: /Revoke key abc123def456/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith(
        `/api/gateway/keys/${KEY.key_hash}/revoke`,
        expect.objectContaining({ method: 'POST' }),
      ),
    )
    vi.unstubAllGlobals()
  })
})
