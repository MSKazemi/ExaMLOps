import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { Prompt } from '@/lib/prompts'
import { Prompts } from './Prompts'

const PROMPTS: Prompt[] = [
  {
    name: 'triage',
    versions: [
      { version: 2, variables: ['ticket'], actor: 'alice', created_at: null },
      { version: 1, variables: ['ticket'], actor: 'alice', created_at: null },
    ],
    labels: [{ label: 'prod', version: 2, updated_at: null }],
  },
]

const apiFetch = vi.fn((path: string, opts?: { method?: string }) => {
  if (path === '/api/prompts' && (!opts || opts.method !== 'POST')) return Promise.resolve(PROMPTS)
  if (path.endsWith('/versions')) return Promise.resolve({ name: 'newp', version: 1, variables: ['x'], label: null })
  return Promise.resolve({ name: 'triage', label: 'prod', version: 1 })
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string, { method?: string }])),
  useMe: () => ({ data: undefined }),
}))

function renderPrompts() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Prompts />
    </QueryClientProvider>,
  )
}

describe('Prompts console — registry (BL-018)', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('shows prompts + labels read-only for viewers (no create form)', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderPrompts()
    await waitFor(() => expect(screen.getByText('triage')).toBeInTheDocument())
    expect(screen.getByText(/prod → v2/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Create version/i })).not.toBeInTheDocument()
  })

  it('lets an admin create a new version through the versions endpoint', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderPrompts()
    fireEvent.change(await screen.findByLabelText('Prompt name'), { target: { value: 'newp' } })
    fireEvent.change(screen.getByLabelText('Prompt template'), { target: { value: 'Hello {x}' } })
    fireEvent.click(screen.getByRole('button', { name: /Create version/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith('/api/prompts/newp/versions', expect.objectContaining({ method: 'POST' })),
    )
    const call = apiFetch.mock.calls.find((c) => c[0] === '/api/prompts/newp/versions')!
    expect(JSON.parse((call[1] as { body: string }).body)).toMatchObject({ template: 'Hello {x}' })
  })

  it('lets an admin move a label (rollback) through the label endpoint', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderPrompts()
    // Point label control for the existing 'triage' prompt.
    fireEvent.change(await screen.findByLabelText('Label version for triage'), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith('/api/prompts/triage/label', expect.objectContaining({ method: 'POST' })),
    )
    const call = apiFetch.mock.calls.find((c) => c[0] === '/api/prompts/triage/label')!
    expect(JSON.parse((call[1] as { body: string }).body)).toMatchObject({ label: 'prod', version: 1 })
  })
})
