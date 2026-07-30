import { describe, it, expect } from 'vitest'
import { fuzzyScore, rankCommands } from './search'

describe('fuzzyScore (mirrors backend score)', () => {
  it('ranks exact > prefix > word-boundary > substring > subsequence > none', () => {
    expect(fuzzyScore('jpcp', 'jpcp')).toBe(100)
    expect(fuzzyScore('jp', 'jpcp')).toBe(80)
    expect(fuzzyScore('cons', 'MLOps Console')).toBe(60)
    expect(fuzzyScore('lops', 'MLOps Console')).toBe(40)
    expect(fuzzyScore('mlc', 'MLOps Console')).toBe(20)
    expect(fuzzyScore('zzz', 'jpcp')).toBe(0)
  })
})

describe('rankCommands (F2 R1/R2)', () => {
  it('returns all commands in registry order for a blank query (viewer)', () => {
    const all = rankCommands('', 'viewer')
    expect(all.length).toBeGreaterThan(0)
    // admin-scoped commands hidden from viewer (F15)
    expect(all.some((c) => c.id === 'nav-audit')).toBe(false)
  })

  it('shows admin-scoped commands to admins', () => {
    const all = rankCommands('', 'admin')
    expect(all.some((c) => c.id === 'nav-audit')).toBe(true)
  })

  it('ranks a matching command first', () => {
    const ranked = rankCommands('facility', 'viewer')
    expect(ranked[0].to).toBe('/operate/facility')
  })

  it('hides admin nav from an unauthenticated (null) role', () => {
    const ranked = rankCommands('approvals', null)
    expect(ranked.some((c) => c.id === 'nav-approvals')).toBe(false)
  })
})
