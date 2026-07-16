import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock the shared fetch layer so we assert the URL/shape without touching the network.
vi.mock('./api', () => ({
  apiFetch: vi.fn(),
}))

import { apiFetch } from './api'
import { listConnections, type ConnectionSummary } from './connections'

const mockFetch = vi.mocked(apiFetch)

describe('listConnections', () => {
  beforeEach(() => {
    mockFetch.mockReset()
  })

  it('calls the project-scoped, URL-encoded connections endpoint', async () => {
    mockFetch.mockResolvedValue([])
    await listConnections('my research')
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/connections?project=my%20research')
  })

  it('returns the connection summaries verbatim (secret never exposed)', async () => {
    const rows: ConnectionSummary[] = [
      {
        name: 'minio-main',
        project: 'research',
        kind: 's3',
        config: { endpoint: 'http://minio:9000' },
        hasSecret: true,
        createdAt: '2026-07-16T10:00:00',
        createdBy: 'alice',
      },
    ]
    mockFetch.mockResolvedValue(rows)
    const out = await listConnections('research')
    expect(out).toEqual(rows)
    expect(out[0].hasSecret).toBe(true)
    // The DTO carries only a boolean flag — never a secret value.
    expect(out[0]).not.toHaveProperty('secret')
  })
})
