/**
 * A failed read of the AI-Act register must not be drawn as an empty register.
 *
 * The backend stopped answering a broken query with `[]` on 2026-09-14 and now answers 503 — but
 * the page defaulted the query to `[]` and rendered "No systems in the register", so the same
 * false all-clear survived one layer up. These assert the page says the register could not be
 * read, and never that it is empty.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { clearAuth, setAuth } from '@/lib/auth'
import { Compliance } from './Compliance'

// `Promise<unknown>`: individual tests below swap in richer payloads, and inferring the return
// type from this first body alone made every one of those a type error.
const apiFetch = vi.fn((path: string, _init?: RequestInit): Promise<unknown> => {
  if (path === '/api/compliance/systems')
    return Promise.reject(
      new Error('the EU-AI-Act system register is unavailable (OperationalError)'),
    )
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

describe('Compliance page — an unreadable register is not an empty one', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('says the register could not be loaded', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText(/Couldn't load the system register/i)).toBeTruthy())
  })

  it('never claims the register is empty when the read failed', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText(/Couldn't load the system register/i)).toBeTruthy())
    expect(screen.queryByText(/No systems in the register/i)).toBeNull()
    expect(screen.queryByText(/No systems classified/i)).toBeNull()
  })

  it('makes the same distinction for an admin, who also sees the editor', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderPage()
    await waitFor(() => expect(screen.getByText(/Couldn't load the system register/i)).toBeTruthy())
    expect(screen.queryByText(/No systems in the register/i)).toBeNull()
  })
})

const SYSTEMS = [
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

const FILE = {
  model: 'JPCP',
  tenant: 'default',
  disclaimer: 'DISCLAIMER: evidence, NOT legal advice.',
  gaps: 0,
  missing: 0,
  insufficient: 0,
  unverified: 0,
  auditChain: '1 chained event(s)',
  telemetryAnchors: '0 anchor(s) checked',
  sections: [
    {
      key: 'changes',
      title: 'Changes',
      annexIv: 'Annex IV §6',
      present: true,
      status: 'verified',
      reasons: [],
      content: 'Change log: 1',
    },
  ],
}

describe('Compliance page — an unreadable version list is not an empty one', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('never says "no saved version yet" when that read failed', async () => {
    // Everything resolves except the saved-versions list, so the page renders far enough to
    // show the panel and the only thing broken is the read under test.
    apiFetch.mockImplementation((path: string) => {
      if (path === '/api/compliance/technical-files/JPCP')
        return Promise.reject(new Error('the technical-file store is unavailable'))
      if (path === '/api/compliance/systems') return Promise.resolve(SYSTEMS)
      if (path === '/api/compliance/technical-file/JPCP') return Promise.resolve(FILE)
      if (path === '/api/compliance/art12/JPCP')
        return Promise.resolve({ model: 'JPCP', total_events: 1, coverage: {}, uncovered: [], coverage_pct: 1 })
      return Promise.resolve({ ok: true })
    })
    renderPage()
    // Wait for the *settled* state, not the heading: the heading renders immediately, so asserting
    // on it raced the query and the panel was still showing its empty state legitimately.
    await waitFor(() => expect(screen.getByText(/could not be read/i)).toBeTruthy())
    expect(screen.queryByText(/No saved version yet/i)).toBeNull()
  })
})

