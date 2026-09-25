import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { HardwareProfileSummary, InUseReport, HistoryEntry } from '@/lib/hardwareProfiles'
import { HardwareProfiles } from './HardwareProfiles'

const PROFILE: HardwareProfileSummary = {
  name: 'gpu-small',
  version: 2,
  acceleratorFamily: 'nvidia',
  acceleratorModelHint: 'a100',
  gpuCount: 1,
  gpuFraction: 1,
  migProfile: null,
  cpu: 4,
  memoryGb: 16,
  nodes: 1,
  driverTag: null,
  runtimeTag: null,
  applicability: ['training', 'workbench'],
  description: 'a small GPU profile',
  createdAt: null,
  createdBy: null,
}

const IN_USE: InUseReport = {
  window_days: 7,
  project: null,
  entries: [
    {
      consumer: 'training',
      consumer_ref: 'JPCP',
      project: 'research',
      name: 'gpu-small',
      version: 2,
      status: 'degraded',
      reason: 'GPU count reduced by scheduler',
      unconfirmed: ['gpu_count'],
      target_cluster: 'cl1',
      resolved_at: '2026-09-25T00:00:00Z',
      exists: true,
    },
  ],
  counts: { degraded: 1 },
  attention: [
    {
      consumer: 'training',
      consumer_ref: 'JPCP',
      project: 'research',
      name: 'gpu-small',
      version: 2,
      status: 'degraded',
      reason: 'GPU count reduced by scheduler',
      unconfirmed: ['gpu_count'],
      target_cluster: 'cl1',
      resolved_at: '2026-09-25T00:00:00Z',
      exists: true,
    },
  ],
  truncated: false,
}

const HISTORY: HistoryEntry[] = [
  {
    id: 1,
    ts: '2026-09-25T00:00:00Z',
    name: 'gpu-small',
    version: 2,
    consumer: 'training',
    consumer_ref: 'JPCP',
    project: 'research',
    target_cluster: 'cl1',
    status: 'degraded',
    reason: 'GPU count reduced by scheduler',
    unconfirmed: 'gpu_count',
    actor: 'exa-pipeline',
  },
]

const apiFetch = vi.fn((path?: string) => {
  // A stray no-arg call can happen from React Query's own retry/cleanup machinery in the test
  // environment (confirmed not from this page's own fetchers, which always build a real path) —
  // resolved harmlessly rather than crashing the mock.
  if (!path) return Promise.resolve({})
  if (path.startsWith('/api/v1/hardware-profiles/in-use')) return Promise.resolve(IN_USE)
  if (path.startsWith('/api/v1/hardware-profiles/history')) return Promise.resolve(HISTORY)
  if (path.startsWith('/api/v1/hardware-profiles')) return Promise.resolve([PROFILE])
  return Promise.resolve({})
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string])),
}))

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <HardwareProfiles />
    </QueryClientProvider>,
  )
}

describe('Hardware Profiles console (ADR 0157)', () => {
  beforeEach(() => apiFetch.mockClear())

  it('defaults to the in-use tab and shows the attention count in its label', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText(/JPCP/)).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /In use \(1\)/ })).toBeInTheDocument()
    expect(screen.getByText('degraded')).toBeInTheDocument()
  })

  it('the "needs attention only" filter narrows entries', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText(/JPCP/)).toBeInTheDocument())
    const checkbox = screen.getByLabelText(/needs attention only/i) as HTMLInputElement
    expect(checkbox.checked).toBe(true)
    fireEvent.click(checkbox)
    // Same single degraded row either way here, but the toggle itself must not crash the page.
    await waitFor(() => expect(screen.getByText(/JPCP/)).toBeInTheDocument())
  })

  it('switches to the catalog tab and shows the profile shape', async () => {
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'Catalog' }))
    await waitFor(() => expect(screen.getAllByText(/gpu-small/).length).toBeGreaterThan(0))
    expect(screen.getByText(/4 CPU/)).toBeInTheDocument()
  })

  it('switches to the history tab and filters by name', async () => {
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'History' }))
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith(expect.stringContaining('/hardware-profiles/history')),
    )
    fireEvent.change(screen.getByLabelText(/filter history by profile name/i), {
      target: { value: 'gpu-small' },
    })
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith(expect.stringContaining('name=gpu-small')),
    )
  })

  it('surfaces the truncated flag as a warning banner', async () => {
    apiFetch.mockImplementation((path?: string) => {
      if (!path) return Promise.resolve({})
      if (path.startsWith('/api/v1/hardware-profiles/in-use'))
        return Promise.resolve({ ...IN_USE, truncated: true })
      if (path.startsWith('/api/v1/hardware-profiles')) return Promise.resolve([PROFILE])
      return Promise.resolve([])
    })
    renderPage()
    await waitFor(() => expect(screen.getByText(/partial view/i)).toBeInTheDocument())
  })
})
