import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { ComplianceSystem } from '@/lib/compliance'
import { ComplianceRegister } from './Governance'

const SYSTEMS: ComplianceSystem[] = [
  {
    model: 'JPCP',
    tenant: 'default',
    in_scope: 1,
    risk_tier: 'high',
    intended_purpose: null,
    deployment_context: null,
    conformity_state: 'draft',
    updated_at: null,
    updated_by: 'alice',
  },
]

const apiFetch = vi.fn((path: string) => {
  if (path === '/api/compliance/systems') return Promise.resolve(SYSTEMS)
  return Promise.resolve({ ok: true })
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string])),
  useMe: () => ({ data: undefined }),
}))

function renderRegister() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ComplianceRegister />
    </QueryClientProvider>,
  )
}

describe('ComplianceRegister — EU AI Act edit parity (BL-016)', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('renders systems read-only for viewers (no tier select)', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderRegister()
    await waitFor(() => expect(screen.getByText('JPCP')).toBeInTheDocument())
    expect(screen.queryByLabelText('Risk tier for JPCP')).not.toBeInTheDocument()
  })

  it('lets an admin change a risk tier through the classify endpoint', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderRegister()
    const select = await screen.findByLabelText('Risk tier for JPCP')
    fireEvent.change(select, { target: { value: 'limited' } })
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith(
        '/api/compliance/classify/JPCP',
        expect.objectContaining({ method: 'POST' }),
      ),
    )
    const call = apiFetch.mock.calls.find((c) => c[0] === '/api/compliance/classify/JPCP')!
    expect(JSON.parse((call[1] as { body: string }).body)).toMatchObject({ riskTier: 'limited' })
  })

  it('lets an admin advance the conformity state', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderRegister()
    const select = await screen.findByLabelText('Conformity state for JPCP')
    fireEvent.change(select, { target: { value: 'documented' } })
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith(
        '/api/compliance/conformity/JPCP',
        expect.objectContaining({ method: 'POST' }),
      ),
    )
  })
})
