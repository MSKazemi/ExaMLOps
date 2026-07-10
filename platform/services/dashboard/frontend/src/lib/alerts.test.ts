import { describe, it, expect } from 'vitest'
import { severityRank, severityToken, inboxHeadline } from './alerts'

describe('severityRank (F12 R1)', () => {
  it('orders critical < error < warn < info', () => {
    expect(severityRank('critical')).toBeLessThan(severityRank('error'))
    expect(severityRank('error')).toBeLessThan(severityRank('warn'))
    expect(severityRank('warn')).toBeLessThan(severityRank('info'))
  })
  it('unknown severities sort last', () => {
    expect(severityRank('mystery')).toBe(9)
  })
})

describe('severityToken (F3)', () => {
  it('maps severities to status tokens', () => {
    expect(severityToken('critical')).toBe('critical')
    expect(severityToken('error')).toBe('critical')
    expect(severityToken('warn')).toBe('warn')
    expect(severityToken('info')).toBe('info')
  })
})

describe('inboxHeadline', () => {
  it('summarizes counts', () => {
    expect(inboxHeadline({ critical: 1, warn: 2 })).toBe('1 critical · 2 warnings')
    expect(inboxHeadline({ error: 1 })).toBe('1 error')
  })
  it('handles an empty inbox', () => {
    expect(inboxHeadline({})).toBe('No active alerts')
  })
})
