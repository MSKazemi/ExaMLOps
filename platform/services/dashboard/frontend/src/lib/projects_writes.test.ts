import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock the shared fetch layer so we assert URL/method/body without touching the network.
vi.mock('./api', () => ({ apiFetch: vi.fn() }))

import { apiFetch } from './api'
import { deleteProject, removeMember, bindStorage } from './projects'

const mockFetch = vi.mocked(apiFetch)

describe('deleteProject', () => {
  beforeEach(() => mockFetch.mockReset())

  it('DELETEs the encoded project path', async () => {
    mockFetch.mockResolvedValue({ name: 'my proj', deleted: true })
    await deleteProject('my proj')
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/projects/my%20proj', { method: 'DELETE' })
  })
})

describe('removeMember', () => {
  beforeEach(() => mockFetch.mockReset())

  it('DELETEs the encoded member sub-resource', async () => {
    mockFetch.mockResolvedValue({ project: 'research', subject: 'alice@x.io', removed: 1 })
    await removeMember('research', 'alice@x.io')
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/v1/projects/research/members/alice%40x.io',
      { method: 'DELETE' },
    )
  })
})

describe('bindStorage', () => {
  beforeEach(() => mockFetch.mockReset())

  it('POSTs a connectionRef when provided', async () => {
    mockFetch.mockResolvedValue({ project: 'research', bucket: 'b', prefix: 'p', connectionRef: 'minio', bound: true })
    await bindStorage('research', { connectionRef: 'minio' })
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/projects/research/storage', {
      method: 'POST',
      body: JSON.stringify({ connectionRef: 'minio' }),
    })
  })

  it('POSTs an empty body to provision storage only', async () => {
    mockFetch.mockResolvedValue({ project: 'research', bucket: 'b', prefix: 'p', connectionRef: null, bound: false })
    await bindStorage('research', {})
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/projects/research/storage', {
      method: 'POST',
      body: JSON.stringify({}),
    })
  })
})
