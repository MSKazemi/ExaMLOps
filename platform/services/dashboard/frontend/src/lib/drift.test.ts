import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('./api', () => ({ apiFetch: vi.fn() }))

import { apiFetch } from './api'
import { setDriftBaseline, resetDrift, setAutoRetrain } from './drift'

const mockFetch = vi.mocked(apiFetch)

describe('setDriftBaseline', () => {
  beforeEach(() => mockFetch.mockReset())
  it('POSTs to the encoded baseline endpoint', async () => {
    mockFetch.mockResolvedValue({ model: 'JPCP', baseline: { mean: 5, n: 20 } })
    await setDriftBaseline('J PCP')
    expect(mockFetch).toHaveBeenCalledWith('/api/drift/baseline/J%20PCP', { method: 'POST' })
  })
})

describe('resetDrift', () => {
  beforeEach(() => mockFetch.mockReset())
  it('POSTs to the encoded reset endpoint', async () => {
    mockFetch.mockResolvedValue({ model: 'JPCP', cleared: 3 })
    await resetDrift('JPCP')
    expect(mockFetch).toHaveBeenCalledWith('/api/drift/reset/JPCP', { method: 'POST' })
  })
})

describe('setAutoRetrain', () => {
  beforeEach(() => mockFetch.mockReset())
  it('POSTs the config body to the encoded endpoint', async () => {
    mockFetch.mockResolvedValue({ enabled: true, dataset: 'PM100Dataset' })
    await setAutoRetrain('JPCP', { enabled: true, dataset: 'PM100Dataset', minZ: 2.5, cooldown: 1800 })
    expect(mockFetch).toHaveBeenCalledWith('/api/drift/auto-retrain/JPCP', {
      method: 'POST',
      body: JSON.stringify({ enabled: true, dataset: 'PM100Dataset', minZ: 2.5, cooldown: 1800 }),
    })
  })
})
