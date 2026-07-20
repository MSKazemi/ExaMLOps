import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('./api', () => ({ apiFetch: vi.fn() }))

import { apiFetch } from './api'
import {
  listProviders, readProvider, saveProvider, validateProvider,
  activateProvider, deleteProvider, providerTemplate,
} from './providers'

const mockFetch = vi.mocked(apiFetch)

describe('providerTemplate', () => {
  it('uses co2e_g for carbon and cost_usd otherwise', () => {
    expect(providerTemplate('carbon')).toContain('co2e_g')
    expect(providerTemplate('cost')).toContain('cost_usd')
    expect(providerTemplate('cost')).toContain('class MyProvider(Provider)')
  })
})

describe('provider fetchers', () => {
  beforeEach(() => mockFetch.mockReset())

  it('listProviders GETs the encoded project', async () => {
    mockFetch.mockResolvedValue([])
    await listProviders('my proj')
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/providers?project=my%20proj')
  })

  it('readProvider GETs the encoded triple', async () => {
    mockFetch.mockResolvedValue({ code: '' })
    await readProvider('research', 'cost', 'my-cost')
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/providers/research/cost/my-cost')
  })

  it('validateProvider POSTs the code', async () => {
    mockFetch.mockResolvedValue({ ok: true })
    await validateProvider('class X(Provider): ...')
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/providers/validate', {
      method: 'POST',
      body: JSON.stringify({ code: 'class X(Provider): ...' }),
    })
  })

  it('saveProvider POSTs the body', async () => {
    mockFetch.mockResolvedValue({})
    await saveProvider({ project: 'research', domain: 'cost', name: 'c', code: 'x', activate: true })
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/providers', {
      method: 'POST',
      body: JSON.stringify({ project: 'research', domain: 'cost', name: 'c', code: 'x', activate: true }),
    })
  })

  it('activateProvider POSTs to the activate path', async () => {
    mockFetch.mockResolvedValue({})
    await activateProvider('research', 'cost', 'c')
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/providers/research/cost/c/activate', { method: 'POST' })
  })

  it('deleteProvider DELETEs the encoded triple', async () => {
    mockFetch.mockResolvedValue({})
    await deleteProvider('research', 'cost', 'c')
    expect(mockFetch).toHaveBeenCalledWith('/api/v1/providers/research/cost/c', { method: 'DELETE' })
  })
})
