import { describe, it, expect } from 'vitest'
import { COMMANDS, visibleCommands } from './commands'
import { HOME_ITEM, NAV_SECTIONS, UTILITY_NAV } from './nav'

const NAV_PATHS = [HOME_ITEM, ...NAV_SECTIONS.flatMap((s) => s.items), ...UTILITY_NAV].map((i) => i.path)

describe('command registry (derived from nav, F2)', () => {
  it('exposes a Navigate command for every sidebar nav item', () => {
    const navTargets = new Set(COMMANDS.filter((c) => c.group === 'Navigate').map((c) => c.to))
    for (const path of NAV_PATHS) {
      expect(navTargets.has(path), `no palette command navigates to ${path}`).toBe(true)
    }
  })

  it('includes the new M1–M5 consoles (searchable via ⌘K)', () => {
    const targets = new Set(COMMANDS.map((c) => c.to))
    for (const p of [
      '/serve/gateway',
      '/build/prompts',
      '/build/features',
      '/operate/autopilot',
      '/operate/slos',
      '/govern/secrets',
      '/govern/fairness',
    ]) {
      expect(targets.has(p), `${p} missing from the command palette`).toBe(true)
    }
  })

  it('keeps stable ids for pre-existing commands', () => {
    const ids = new Set(COMMANDS.map((c) => c.id))
    expect(ids.has('nav-audit')).toBe(true)
    expect(ids.has('nav-approvals')).toBe(true)
    expect(ids.has('nav-overview')).toBe(true)
  })

  it('has no duplicate command ids', () => {
    const ids = COMMANDS.map((c) => c.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('scopes admin-only nav items (Govern) to admins', () => {
    const secrets = COMMANDS.find((c) => c.to === '/govern/secrets')!
    expect(secrets.scopes).toEqual(['admin'])
    expect(visibleCommands('viewer').some((c) => c.to === '/govern/secrets')).toBe(false)
    expect(visibleCommands('admin').some((c) => c.to === '/govern/secrets')).toBe(true)
  })
})
