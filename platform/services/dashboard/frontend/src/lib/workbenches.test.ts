import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock the shared fetch layer so we assert the URL/shape/body without touching the network.
vi.mock('./api', () => ({
  apiFetch: vi.fn(),
}))

import { apiFetch } from './api'
import {
  listWorkbenches,
  setWorkbenchStatus,
  nextStatus,
  type WorkbenchSummary,
} from './workbenches'

const mockFetch = vi.mocked(apiFetch)

describe('nextStatus', () => {
  it('toggles RUNNING ↔ STOPPED', () => {
    expect(nextStatus('RUNNING')).toBe('STOPPED')
    expect(nextStatus('STOPPED')).toBe('RUNNING')
  })
})

describe('listWorkbenches', () => {
  beforeEach(() => {
    mockFetch.mockReset()
  })

  it('calls the project-scoped, URL-encoded workbenches endpoint', async () => {
    mockFetch.mockResolvedValue([])
    await listWorkbenches('my research')
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/workbenches?project=my%20research')
  })

  it('returns the workbench summaries verbatim', async () => {
    const rows: WorkbenchSummary[] = [
      {
        name: 'nb-alice',
        project: 'research',
        image: 'jupyter/scipy-notebook',
        cpu: 4,
        memoryGb: 8,
        volume: 'nb-alice-vol',
        status: 'RUNNING',
        createdAt: '2026-07-16T10:00:00',
        createdBy: 'alice',
      },
    ]
    mockFetch.mockResolvedValue(rows)
    expect(await listWorkbenches('research')).toEqual(rows)
  })
})

describe('setWorkbenchStatus', () => {
  beforeEach(() => {
    mockFetch.mockReset()
  })

  it('POSTs the new status to the project/name-scoped endpoint', async () => {
    mockFetch.mockResolvedValue({ project: 'research', name: 'nb-alice', status: 'STOPPED' })
    await setWorkbenchStatus('research', 'nb-alice', 'STOPPED')
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/workbenches/research/nb-alice/status', {
      method: 'POST',
      body: JSON.stringify({ status: 'STOPPED' }),
    })
  })

  it('URL-encodes the project and workbench names', async () => {
    mockFetch.mockResolvedValue({ project: 'my research', name: 'nb alice', status: 'RUNNING' })
    await setWorkbenchStatus('my research', 'nb alice', 'RUNNING')
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/v1/workbenches/my%20research/nb%20alice/status',
      { method: 'POST', body: JSON.stringify({ status: 'RUNNING' }) },
    )
  })
})
