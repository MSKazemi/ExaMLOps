import { describe, it, expect } from 'vitest'
import { waitLabel, partitionTone, clusterStateTone, type PartitionUtil } from './facility'

describe('waitLabel', () => {
  it('formats seconds / minutes / hours', () => {
    expect(waitLabel(30)).toBe('30s')
    expect(waitLabel(120)).toBe('2m')
    expect(waitLabel(3660)).toBe('1h 1m')
    expect(waitLabel(7200)).toBe('2h')
  })
})

describe('partitionTone (F6 R1)', () => {
  const base: PartitionUtil = { name: 'slurm', running: 0, queued: 0, gpusAllocated: 0 }
  it('is unknown when idle (no data)', () => {
    expect(partitionTone(base)).toBe('unknown')
  })
  it('warns when the queue exceeds running work', () => {
    expect(partitionTone({ ...base, running: 1, queued: 3 })).toBe('warn')
  })
  it('is ok when running keeps up with the queue', () => {
    expect(partitionTone({ ...base, running: 4, queued: 1 })).toBe('ok')
  })
})

describe('clusterStateTone (F6 / Phase 35b)', () => {
  it('maps approval states to colourblind-safe status tokens', () => {
    expect(clusterStateTone('ACTIVE')).toBe('healthy')
    expect(clusterStateTone('PENDING')).toBe('pending')
    expect(clusterStateTone('REJECTED')).toBe('failed')
  })
})
