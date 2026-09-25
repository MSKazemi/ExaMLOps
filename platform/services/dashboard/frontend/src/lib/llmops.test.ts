import { describe, it, expect } from 'vitest'
import { passRateLabel, evalTone, providerOriginLabel, type CalculationProvider } from './llmops'

describe('providerOriginLabel (ADR 0083)', () => {
  const base: CalculationProvider = {
    domain: 'llm_cost', provider: 'token-rate', selected: false, default: 'token-rate', ok: true, error: null,
  }
  it('distinguishes default, operator-configured and broken providers', () => {
    expect(providerOriginLabel(base)).toBe('default')
    expect(providerOriginLabel({ ...base, selected: true })).toBe('configured')
    expect(providerOriginLabel({ ...base, selected: true, ok: false, error: 'boom' })).toBe('failed to load')
  })
  it('labels the caller\'s own arithmetic as built-in, not as the default provider', () => {
    expect(providerOriginLabel({ ...base, provider: null, mode: 'builtin' })).toBe('built-in')
    expect(providerOriginLabel({ ...base, mode: 'provider' })).toBe('default')
  })
})

describe('passRateLabel (F10 R1)', () => {
  it('formats a ratio and handles no-evals', () => {
    expect(passRateLabel(0.5)).toBe('50%')
    expect(passRateLabel(1)).toBe('100%')
    expect(passRateLabel(null)).toBe('no evals')
  })
})

describe('evalTone', () => {
  it('grades all/some/none passing', () => {
    expect(evalTone(1)).toBe('ok')
    expect(evalTone(0.5)).toBe('warn')
    expect(evalTone(0)).toBe('critical')
    expect(evalTone(null)).toBe('unknown')
  })
})
