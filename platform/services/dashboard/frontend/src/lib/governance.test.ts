import { describe, it, expect } from 'vitest'
import { postureToken, coverageLabel, digestShort } from './governance'

describe('postureToken (F14 R1)', () => {
  it('maps posture status to F3 tokens', () => {
    expect(postureToken('satisfied')).toBe('ok')
    expect(postureToken('partial')).toBe('warn')
    expect(postureToken('gap')).toBe('critical')
  })
})

describe('coverageLabel', () => {
  it('formats a ratio and handles null honestly', () => {
    expect(coverageLabel(0.5)).toBe('50%')
    expect(coverageLabel(1)).toBe('100%')
    expect(coverageLabel(null)).toBe('no models')
  })
})

describe('digestShort (F14 R3)', () => {
  it('truncates the head digest', () => {
    expect(digestShort('0123456789abcdef0000')).toBe('0123456789ab')
    expect(digestShort(null)).toBe('—')
  })
})
