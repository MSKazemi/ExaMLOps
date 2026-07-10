import { describe, it, expect } from 'vitest'
import { scrubPii } from './telemetry'

describe('scrubPii (F24 R1 / F16)', () => {
  it('redacts email addresses', () => {
    expect(scrubPii('contact alice@example.com now')).toBe('contact [email] now')
  })
  it('redacts bearer tokens and JWTs', () => {
    expect(scrubPii('Authorization: Bearer abc.def-123')).toContain('Bearer [redacted]')
    expect(scrubPii('token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9')).toContain('[token]')
  })
  it('redacts long hex ids', () => {
    expect(scrubPii('run 0123456789abcdef0123456789abcdef')).toBe('run [hex]')
  })
  it('leaves ordinary text untouched', () => {
    expect(scrubPii('promotion blocked: no policy')).toBe('promotion blocked: no policy')
  })
  it('handles empty input', () => {
    expect(scrubPii('')).toBe('')
  })
})
