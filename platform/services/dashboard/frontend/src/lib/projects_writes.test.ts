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

describe('updateProject', () => {
  beforeEach(() => mockFetch.mockReset())

  it('PUTs the editable fields to the encoded project path', async () => {
    mockFetch.mockResolvedValue({ name: 'research', quotaUpdated: true, budgetUpdated: true })
    const { updateProject } = await import('./projects')
    await updateProject('research', { cpuLimit: 16, networkName: 'ns', gpuHoursBudget: 100 })
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/projects/research', {
      method: 'PUT',
      body: JSON.stringify({ cpuLimit: 16, networkName: 'ns', gpuHoursBudget: 100 }),
    })
  })
})

describe('model-zoo onboarding', () => {
  beforeEach(() => mockFetch.mockReset())

  it('lists zoo models (GET)', async () => {
    mockFetch.mockResolvedValue({ models: [{ model: 'JPCP', project: 'jpcp' }] })
    const { onboardModel, onboardAllModels, listZooModels } = await import('./projects')
    await listZooModels()
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/projects/zoo-models')

    mockFetch.mockResolvedValue({ model: 'JPCP', project: 'jpcp', changed: true, steps: {} })
    await onboardModel('JPCP', { dryRun: true })
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/projects/onboard/JPCP', {
      method: 'POST',
      body: JSON.stringify({ dryRun: true }),
    })

    mockFetch.mockResolvedValue({ results: [], onboarded: 0 })
    await onboardAllModels({})
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/projects/onboard-all', {
      method: 'POST',
      body: JSON.stringify({}),
    })
  })
})
