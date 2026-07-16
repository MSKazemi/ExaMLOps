import { describe, it, expect } from 'vitest'
import { placementTone, privacyLabel, burstTone, poolCostLabel } from './nextgen'

describe('placementTone (E8)', () => {
  it('maps placement decisions to colourblind-safe tokens', () => {
    expect(placementTone('placed')).toBe('ok')
    expect(placementTone('fallback')).toBe('warn')
    expect(placementTone('rejected')).toBe('error')
    expect(placementTone('weird')).toBe('unknown')
  })
})

describe('privacyLabel (E7)', () => {
  it('is honest when DP + secure-agg are on', () => {
    expect(privacyLabel({ dp_enabled: 1, secure_agg: 1, epsilon: 0.5 })).toBe('DP ε=0.50 · secure-agg')
  })
  it('claims no privacy when both are off', () => {
    expect(privacyLabel({ dp_enabled: 0, secure_agg: 0, epsilon: 0 })).toBe('no DP · updates visible')
  })
  it('reflects partial config', () => {
    expect(privacyLabel({ dp_enabled: 1, secure_agg: 0, epsilon: 1.25 })).toBe('DP ε=1.25 · updates visible')
  })
})

describe('burstTone (E8)', () => {
  it('is error when a burst is blocked by governance', () => {
    expect(burstTone(0)).toBe('error')
    expect(burstTone(1)).toBe('ok')
  })
})

describe('poolCostLabel (E8)', () => {
  it('renders per-device-hour cost + carbon', () => {
    expect(poolCostLabel({ cost_per_hour: 1.8, carbon_factor: 250 })).toBe('$1.80/hr · 250 gCO₂e/hr')
  })
})
