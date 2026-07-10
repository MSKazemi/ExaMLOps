import { describe, it, expect } from 'vitest'
import { passRateLabel, evalTone } from './llmops'

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
