import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { SecretMeta } from '@/lib/secrets'
import { Secrets } from './Secrets'

const SECRETS: SecretMeta[] = [
  { path: 'svc/token', tenant: 'default', version: 2, updated_by: 'alice', updated_at: null, hasValue: true },
]

const apiFetch = vi.fn((path: string, opts?: { method?: string }) => {
  if (path === '/api/secrets' && (!opts || opts.method !== 'POST')) return Promise.resolve(SECRETS)
  return Promise.resolve({ path: 'svc/new', tenant: 'default', version: 1 })
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string, { method?: string }])),
  useMe: () => ({ data: undefined }),
}))

function renderSecrets() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Secrets />
    </QueryClientProvider>,
  )
}

describe('Secrets console (BL-021)', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('lists metadata only and hides the set form from viewers', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderSecrets()
    await waitFor(() => expect(screen.getByText('svc/token')).toBeInTheDocument())
    expect(screen.getByText('v2')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Save secret/i })).not.toBeInTheDocument()
  })

  it('sends a value via a password field and clears it after (write-only)', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderSecrets()
    const valueField = (await screen.findByLabelText('Secret value')) as HTMLInputElement
    expect(valueField.type).toBe('password') // never a visible field
    fireEvent.change(await screen.findByLabelText('Secret path'), { target: { value: 'svc/new' } })
    fireEvent.change(valueField, { target: { value: 'hunter2' } })
    fireEvent.click(screen.getByRole('button', { name: /Save secret/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith('/api/secrets', expect.objectContaining({ method: 'POST' })),
    )
    const call = apiFetch.mock.calls.find((c) => c[0] === '/api/secrets' && (c[1] as { method?: string })?.method === 'POST')!
    expect(JSON.parse((call[1] as { body: string }).body)).toMatchObject({ path: 'svc/new', value: 'hunter2' })
    // The value field is cleared after a successful save (no lingering plaintext).
    await waitFor(() => expect((screen.getByLabelText('Secret value') as HTMLInputElement).value).toBe(''))
  })
})
