import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock the shared fetch layer so we assert the URL without touching the network.
vi.mock('./api', () => ({
  apiFetch: vi.fn(),
}))

import { apiFetch } from './api'
import {
  getInUse,
  isApplicable,
  listHardwareProfiles,
  needsAttention,
  profileShape,
  type HardwareProfileSummary,
} from './hardwareProfiles'
import { createWorkbench } from './workbenches'

const base: HardwareProfileSummary = {
  name: 'gpu-small',
  version: 2,
  acceleratorFamily: 'nvidia',
  acceleratorModelHint: null,
  gpuCount: 1,
  gpuFraction: 1,
  migProfile: null,
  cpu: 4,
  memoryGb: 16,
  nodes: 1,
  driverTag: null,
  runtimeTag: null,
  applicability: ['training', 'workbench'],
  description: '',
  createdAt: null,
  createdBy: null,
}

describe('hardware profile helpers', () => {
  it('describes the whole shape, including a fractional GPU', () => {
    expect(profileShape(base)).toBe('gpu-small v2 · 4 CPU · 16 GB · 1 GPU')
    expect(profileShape({ ...base, gpuFraction: 0.5 })).toBe('gpu-small v2 · 4 CPU · 16 GB · 1×0.5 GPU')
  })

  it('omits dimensions the profile does not ask for', () => {
    expect(profileShape({ ...base, gpuCount: 0, memoryGb: 0 })).toBe('gpu-small v2 · 4 CPU')
  })

  it('applies the same applicability rule as the backend (any covers everything)', () => {
    expect(isApplicable(base, 'workbench')).toBe(true)
    expect(isApplicable(base, 'serving')).toBe(false)
    expect(isApplicable({ ...base, applicability: ['any'] }, 'serving')).toBe(true)
  })

  it('flags degraded, unresolvable and missing — never unchecked or verified', () => {
    expect(needsAttention('degraded')).toBe(true)
    expect(needsAttention('unresolvable')).toBe(true)
    expect(needsAttention('missing')).toBe(true)
    expect(needsAttention('unchecked')).toBe(false)
    expect(needsAttention('verified')).toBe(false)
  })
})

describe('hardware profile fetchers', () => {
  beforeEach(() => vi.mocked(apiFetch).mockReset())

  it('filters the catalog by applicability', async () => {
    vi.mocked(apiFetch).mockResolvedValue([])
    await listHardwareProfiles('workbench')
    expect(apiFetch).toHaveBeenCalledWith('/api/v1/hardware-profiles?applicability=workbench')
  })

  it('scopes in-use to one project', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ entries: [] })
    await getInUse('research lab')
    expect(apiFetch).toHaveBeenCalledWith('/api/v1/hardware-profiles/in-use?project=research%20lab')
  })

  it('sends the chosen profile on workbench create', async () => {
    vi.mocked(apiFetch).mockResolvedValue({})
    await createWorkbench('research', { name: 'nb', hardwareProfile: 'gpu-small' })
    const [, init] = vi.mocked(apiFetch).mock.calls[0]
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      project: 'research',
      name: 'nb',
      hardwareProfile: 'gpu-small',
    })
  })
})
