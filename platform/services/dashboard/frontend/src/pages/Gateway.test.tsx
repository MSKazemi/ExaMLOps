import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { GatewayStatus, VirtualKey } from '@/lib/gateway'
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

const READY_STATUS: GatewayStatus = {
  reachable: true,
  ready: true,
  healthyNow: true,
  routes: { default: { healthy: true, required: true, deployments: 1 } },
  warnings: [],
}

const UNREACHABLE_STATUS: GatewayStatus = {
  reachable: false,
  ready: false,
  healthyNow: false,
  routes: {},
  warnings: [],
}

const apiFetch = vi.fn((path: string, opts?: { method?: string; body?: string }) => {
  if (path === '/api/gateway/keys' && (!opts || opts.method !== 'POST')) return Promise.resolve([KEY])
  if (path === '/api/gateway/keys' && opts?.method === 'POST')
    return Promise.resolve({ key: 'exa-RAWSECRET123', tenant: 'default', project: 'research', models: [], budgetUsd: null })
  if (path === '/api/gateway/status') return Promise.resolve(READY_STATUS)
  if (path === '/api/gateway/test-chat')
    return Promise.resolve({ ok: true, status: 200, latencyMs: 42, reply: 'pong' })
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

  it('shows live status from the deployed gateway to viewers, with no test-chat form', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderGateway()
    await waitFor(() => expect(screen.getByText('Ready')).toBeInTheDocument())
    expect(screen.getByText(/1\/1 routes healthy/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Send/i })).not.toBeInTheDocument()
  })

  it('sends a test chat message and shows the reply (admin only)', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderGateway()
    const input = await screen.findByLabelText('Test message')
    fireEvent.change(input, { target: { value: 'ping' } })
    fireEvent.click(screen.getByRole('button', { name: /Send/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith(
        '/api/gateway/test-chat',
        expect.objectContaining({ method: 'POST' }),
      ),
    )
    expect(await screen.findByText('pong')).toBeInTheDocument()
    const [, opts] = apiFetch.mock.calls.find(([p]) => p === '/api/gateway/test-chat')!
    expect(JSON.parse((opts as { body: string }).body)).toMatchObject({ message: 'ping', route: 'default' })
  })

  it('reports an unreachable gateway without crashing the page', async () => {
    apiFetch.mockImplementation((path: string) => {
      if (path === '/api/gateway/status') return Promise.resolve(UNREACHABLE_STATUS)
      return Promise.resolve([KEY])
    })
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderGateway()
    await waitFor(() => expect(screen.getByText('Unreachable')).toBeInTheDocument())
    expect(screen.getByText(/exa gateway providers/)).toBeInTheDocument()
  })
})
