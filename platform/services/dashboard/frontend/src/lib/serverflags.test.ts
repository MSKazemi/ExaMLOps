import { describe, it, expect } from 'vitest'
import { flagFallback } from './serverflags'

describe('flagFallback (F25 R2 — client fallback)', () => {
  it('returns a known flag default when the server has not answered', () => {
    // mlopsConsole defaults to true in the client registry
    expect(flagFallback('mlopsConsole')).toBe(true)
  })
  it('is off for a flag the client does not know', () => {
    expect(flagFallback('totallyUnknownFlag')).toBe(false)
  })
})
