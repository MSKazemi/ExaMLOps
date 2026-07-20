import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('./api', () => ({ apiFetch: vi.fn() }))

import { apiFetch } from './api'
import { getModelTraffic, setModelTraffic, weightSum } from './traffic'

const mockFetch = vi.mocked(apiFetch)

describe('weightSum', () => {
  it('sums the weight map', () => {
    expect(weightSum({ Production: 90, Canary: 10 })).toBe(100)
    expect(weightSum({})).toBe(0)
  })
})

describe('getModelTraffic', () => {
  beforeEach(() => mockFetch.mockReset())
  it('GETs the encoded model path', async () => {
    mockFetch.mockResolvedValue(null)
    await getModelTraffic('J PCP')
    expect(mockFetch).toHaveBeenCalledWith('/api/platform-data/traffic-rules/J%20PCP')
  })
})

describe('setModelTraffic', () => {
  beforeEach(() => mockFetch.mockReset())
  it('PUTs the rules body to the encoded model path', async () => {
    mockFetch.mockResolvedValue({ model: 'JPCP', rules: { Production: 100 } })
    await setModelTraffic('JPCP', { Production: 100 })
    expect(mockFetch).toHaveBeenCalledWith('/api/platform-data/traffic-rules/JPCP', {
      method: 'PUT',
      body: JSON.stringify({ rules: { Production: 100 } }),
    })
  })
})
