import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { ComplianceSystem, TechnicalFile } from '@/lib/compliance'
import { Compliance } from './Compliance'

const SYSTEMS: ComplianceSystem[] = [
  {
    model: 'JPCP',
    tenant: 'default',
    in_scope: 1,
    risk_tier: 'high',
    intended_purpose: 'HPC power prediction',
    deployment_context: 'internal',
    conformity_state: 'draft',
    updated_at: null,
    updated_by: 'alice',
  },
]

const FILE: TechnicalFile = {
  model: 'JPCP',
  tenant: 'default',
  disclaimer: 'DISCLAIMER: evidence, NOT legal advice.',
  gaps: 2,
  missing: 1,
  insufficient: 1,
  unverified: 1,
  auditChain: 'BROKEN at event 7 (hash mismatch (event altered)); 12 chained event(s)',
  telemetryAnchors: '3 anchor(s) checked',
  sections: [
    { key: 'changes', title: 'Changes', annexIv: 'Annex IV §6', present: true, status: 'insufficient',
      reasons: ['audit chain is broken at event 7 — events after it cannot be relied on'], content: 'Change log: 12' },
    { key: 'fairness', title: 'Fairness', annexIv: 'Annex IV §3', present: false, status: 'missing',
      reasons: [], content: 'No fairness report' },
    { key: 'system_description', title: 'System Description', annexIv: 'Annex IV §1', present: true,
      status: 'unverified', reasons: ['compliance_systems is outside the hash chain and the telemetry anchors'],
      content: 'Intended purpose: HPC power prediction' },
    { key: 'record_keeping', title: 'Record Keeping', annexIv: 'Art. 12', present: true, status: 'verified',
      reasons: [], content: 'Art. 12 audit coverage' },
  ],
}

const apiFetch = vi.fn((path: string, init?: RequestInit) => {
  if (path === '/api/compliance/systems') return Promise.resolve(SYSTEMS)
  if (path === '/api/compliance/technical-file/JPCP' && init?.method === 'POST')
    return Promise.resolve({ model: 'JPCP', version: 1, gaps: 2 })
  if (path === '/api/compliance/technical-file/JPCP') return Promise.resolve(FILE)
  if (path === '/api/compliance/technical-files/JPCP') return Promise.resolve([])
  if (path === '/api/compliance/art12/JPCP')
    return Promise.resolve({ model: 'JPCP', total_events: 3, coverage: { promotion: true, approval: false },
      uncovered: ['approval'], coverage_pct: 0.5 })
  return Promise.resolve({ ok: true })
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string, RequestInit?])),
  useMe: () => ({ data: undefined }),
}))

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Compliance />
    </QueryClientProvider>,
  )
}

describe('Compliance page — technical file with evidence sufficiency (ADR 0012 cl.4, ADR 0110)', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('opens with what the file cannot vouch for, with reasons', async () => {
    renderPage()
    const region = await screen.findByRole('region', { name: 'Insufficient evidence' })
    expect(within(region).getByText('Changes')).toBeInTheDocument()
    expect(within(region).getByText(/audit chain is broken at event 7/)).toBeInTheDocument()
    expect(within(region).getByText('Fairness')).toBeInTheDocument()
    expect(within(region).queryByText('Record Keeping')).not.toBeInTheDocument() // verified
    expect(screen.getByText(/BROKEN at event 7/)).toBeInTheDocument()
    expect(screen.getByText(/NOT legal advice/)).toBeInTheDocument()
  })

  it('labels every status in text, not colour alone', async () => {
    renderPage()
    await screen.findByRole('region', { name: 'Insufficient evidence' })
    expect(screen.getAllByLabelText('Insufficient').length).toBeGreaterThan(0)
    expect(screen.getAllByLabelText('Not tamper-evident').length).toBeGreaterThan(0)
    expect(screen.getAllByLabelText('Verified').length).toBeGreaterThan(0)
    expect(await screen.findByLabelText('approval: not recorded')).toBeInTheDocument() // own query
  })

  it('hides Save for viewers and saves a version for admins', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    const { unmount } = renderPage()
    await screen.findByRole('region', { name: 'Insufficient evidence' })
    expect(screen.queryByRole('button', { name: /save version/i })).not.toBeInTheDocument()
    unmount()
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /save version/i }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith('/api/compliance/technical-file/JPCP', { method: 'POST' }),
    )
  })
})
