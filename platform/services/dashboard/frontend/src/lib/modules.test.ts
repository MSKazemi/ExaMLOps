import { describe, expect, it } from 'vitest'
import { pageAllowed } from './modules'

describe('pageAllowed (site feature profile, ADR 0128)', () => {
  it('allows everything when the profile is unknown or nothing is off', () => {
    expect(pageAllowed('/operate/finops', undefined)).toBe(true)
    expect(pageAllowed('/operate/finops', [])).toBe(true)
  })

  it('hides a page of a disabled module and the pages under it', () => {
    const off = ['/operate/finops', '/serve/llmops']
    expect(pageAllowed('/operate/finops', off)).toBe(false)
    expect(pageAllowed('/operate/finops/budgets', off)).toBe(false)
    expect(pageAllowed('/serve/llmops', off)).toBe(false)
  })

  it('matches whole path segments, never a prefix inside a segment', () => {
    expect(pageAllowed('/operate/finops-archive', ['/operate/finops'])).toBe(true)
    expect(pageAllowed('/operate/drift', ['/operate/finops'])).toBe(true)
  })
})
