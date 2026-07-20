import { describe, it, expect } from 'vitest'
import {
  statusToken,
  quotaSummary,
  budgetUsage,
  storageUsagePct,
  bytesToGb,
  pipelineToken,
  type ProjectStorage,
  type ProjectQuota,
  type ProjectBudget,
  type ProjectConsumption,
} from './projects'

describe('statusToken', () => {
  it('maps project status to F3 tokens', () => {
    expect(statusToken('active')).toBe('ok')
    expect(statusToken('suspended')).toBe('warn')
    expect(statusToken('archived')).toBe('critical')
  })
  it('falls back to unknown for anything unrecognised', () => {
    expect(statusToken('weird')).toBe('unknown')
    expect(statusToken('')).toBe('unknown')
  })
})

describe('quotaSummary', () => {
  it('renders a one-line CPU/mem/storage/GPU summary', () => {
    const q: ProjectQuota = { cpuLimit: 32, memoryLimitGb: 128, storageGb: 500, gpuLimit: 4 }
    expect(quotaSummary(q)).toBe('32 CPU · 128 GB · 500 GB · 4 GPU')
  })
})

describe('budgetUsage', () => {
  const consumption: ProjectConsumption = { gpu_hours: 25, cost_usd: 100 }

  it('returns null when no budget is set', () => {
    expect(budgetUsage(null, consumption)).toBeNull()
  })
  it('returns null when the GPU-hour budget is non-positive', () => {
    const budget: ProjectBudget = { gpuHours: 0, costUsd: 0 }
    expect(budgetUsage(budget, consumption)).toBeNull()
  })
  it('computes the consumed ratio against the GPU-hour budget', () => {
    const budget: ProjectBudget = { gpuHours: 100, costUsd: 500 }
    expect(budgetUsage(budget, consumption)).toBeCloseTo(0.25)
  })
})

describe('storageUsagePct (P6)', () => {
  const base: ProjectStorage = { bucket: 'examlops-projects', prefix: 'demo/', quotaGb: 100, usedBytes: 0, connectionRef: null }
  it('is 0 with no usage', () => {
    expect(storageUsagePct(base)).toBe(0)
  })
  it('computes a percentage of the GB quota', () => {
    expect(storageUsagePct({ ...base, usedBytes: 25e9 })).toBe(25)
  })
  it('caps at 100 and is 0 without a quota', () => {
    expect(storageUsagePct({ ...base, usedBytes: 500e9 })).toBe(100)
    expect(storageUsagePct({ ...base, quotaGb: 0, usedBytes: 5e9 })).toBe(0)
  })
})

describe('bytesToGb', () => {
  it('formats bytes as GB', () => {
    expect(bytesToGb(1_500_000_000)).toBe('1.50 GB')
  })
})

describe('pipelineToken (P7)', () => {
  it('maps pipeline status to colourblind-safe tokens', () => {
    expect(pipelineToken('healthy')).toBe('ok')
    expect(pipelineToken('degraded')).toBe('warn')
    expect(pipelineToken('unknown')).toBe('unknown')
  })
})
