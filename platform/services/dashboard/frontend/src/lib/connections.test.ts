import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock the shared fetch layer so we assert the URL/shape without touching the network.
vi.mock('./api', () => ({
  apiFetch: vi.fn(),
}))

import { apiFetch } from './api'
import {
  listConnections, createConnection, deleteConnection, testConnection,
  type ConnectionSummary, type CreateConnectionBody,
} from './connections'

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

describe('createConnection', () => {
  beforeEach(() => mockFetch.mockReset())

  it('POSTs the create body to the connections endpoint', async () => {
    mockFetch.mockResolvedValue({ name: 'minio', kind: 's3' })
    const body: CreateConnectionBody = {
      name: 'minio', kind: 's3', project: 'research',
      config: { endpoint: 'http://minio:9000' }, secret: 'top-secret',
    }
    await createConnection(body)
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/connections', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  })
})

describe('deleteConnection', () => {
  beforeEach(() => mockFetch.mockReset())

  it('DELETEs a project-scoped connection with an encoded project query', async () => {
    mockFetch.mockResolvedValue({ name: 'minio', project: 'my research', deleted: true })
    await deleteConnection('minio', 'my research')
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/connections/minio?project=my%20research', {
      method: 'DELETE',
    })
  })

  it('omits the project query for a global connection', async () => {
    mockFetch.mockResolvedValue({ name: 'g', project: null, deleted: true })
    await deleteConnection('g', null)
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/connections/g', { method: 'DELETE' })
  })
})

describe('testConnection', () => {
  beforeEach(() => mockFetch.mockReset())

  it('POSTs to the /test sub-resource', async () => {
    mockFetch.mockResolvedValue({ name: 'minio', project: 'research', ok: true, detail: 'reachable' })
    const r = await testConnection('minio', 'research')
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/connections/minio/test?project=research', {
      method: 'POST',
    })
    expect(r.ok).toBe(true)
  })
})
