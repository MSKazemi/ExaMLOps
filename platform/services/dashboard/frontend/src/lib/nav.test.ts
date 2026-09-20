import { describe, it, expect } from 'vitest'
import {
  HOME_ITEM,
  NAV_SECTIONS,
  UTILITY_NAV,
  ROUTE_REDIRECTS,
  activeSectionId,
  isNavItemActive,
  pageTitleForPath,
} from './nav'

/**
 * Canonical routes registered in App.tsx (leaf paths; detail routes reduced to their prefix). The nav
 * must never point at a route that does not exist — this list is the contract the grouped shell honours.
 */
const KNOWN_ROUTES = new Set([
  '/',
  '/preferences',
  '/documents',
  // build
  '/build/models',
  '/build/mlops',
  '/build/datasets',
  '/build/features',
  '/build/assets',
  '/build/pipelines',
  '/build/prompts',
  // serve
  '/serve/llmops',
  '/serve/traffic',
  '/serve/gateway',
  '/serve/scaling',
  '/serve/nextgen',
  // operate
  '/operate/drift',
  '/operate/alerts',
  '/operate/autopilot',
  '/operate/agent-runs',
  '/operate/slos',
  '/operate/admission',
  '/operate/facility',
  '/operate/finops',
  '/operate/self-obs',
  // govern
  '/govern/governance',
  '/govern/compliance',
  '/govern/audit',
  '/govern/approvals',
  '/govern/fairness',
  '/govern/secrets',
  // platform
  '/platform/cli',
  '/platform/resources',
  '/platform/projects',
  '/platform/ops',
  '/platform/events',
  '/platform/services',
  '/platform/providers',
  '/platform/config',
  '/platform/jupyter',
  '/platform/flags',
])

const ALL_ITEMS = [HOME_ITEM, ...NAV_SECTIONS.flatMap((s) => s.items), ...UTILITY_NAV]

describe('nav config', () => {
  it('exposes the six lifecycle groups in order', () => {
    expect(NAV_SECTIONS.map((s) => s.id)).toEqual(['build', 'serve', 'operate', 'govern', 'platform'])
  })

  it('every nav item points at a canonical route registered in App.tsx', () => {
    for (const item of ALL_ITEMS) {
      expect(KNOWN_ROUTES.has(item.path), `${item.path} is not a registered route`).toBe(true)
    }
  })

  it('scopes each group item under its lifecycle prefix', () => {
    for (const section of NAV_SECTIONS) {
      for (const item of section.items) {
        expect(item.path.startsWith(`/${section.id}/`), `${item.path} not under /${section.id}/`).toBe(true)
      }
    }
  })

  it('has no duplicate paths across Home, sections and utility', () => {
    const paths = ALL_ITEMS.map((i) => i.path)
    expect(new Set(paths).size).toBe(paths.length)
  })

  it('gives every item a label and an icon, and every section a label/icon/items', () => {
    for (const item of ALL_ITEMS) {
      expect(item.label).toBeTruthy()
      expect(item.icon).toBeTruthy()
    }
    for (const s of NAV_SECTIONS) {
      expect(s.label).toBeTruthy()
      expect(s.icon).toBeTruthy()
      expect(s.items.length).toBeGreaterThan(0)
    }
  })

  it('marks the governance group admin-only and tags the approvals badge', () => {
    const govern = NAV_SECTIONS.find((s) => s.id === 'govern')!
    expect(govern.items.every((i) => i.adminOnly)).toBe(true)
    expect(govern.items.find((i) => i.path === '/govern/approvals')?.badge).toBe('approvals')
  })

  it('keeps flag-gated consoles behind their flags (matches App.tsx routing)', () => {
    const byPath = Object.fromEntries(NAV_SECTIONS.flatMap((s) => s.items).map((i) => [i.path, i]))
    expect(byPath['/build/mlops'].flag).toBe('mlopsConsole')
    expect(byPath['/operate/facility'].flag).toBe('facilityConsole')
    expect(byPath['/platform/projects'].flag).toBe('projectsConsole')
    expect(byPath['/platform/cli'].flag).toBe('cliConsole')
    expect(byPath['/platform/resources'].flag).toBe('cliConsole')
  })
})

describe('ROUTE_REDIRECTS (clean-slate URL migration)', () => {
  it('maps each old flat path to a canonical route', () => {
    for (const [from, to] of Object.entries(ROUTE_REDIRECTS)) {
      expect(from.split('/').filter(Boolean)).toHaveLength(1) // old paths are single-segment
      expect(KNOWN_ROUTES.has(to), `${from} → ${to} is not a canonical route`).toBe(true)
      expect(from).not.toBe(to)
    }
  })

  it('covers every relocated console (old flat path has a redirect)', () => {
    // Every group item that moved under a lifecycle prefix should have a back-compat redirect,
    // except the one whose flat name differs (status→self-obs) — covered explicitly.
    expect(ROUTE_REDIRECTS['/models']).toBe('/build/models')
    expect(ROUTE_REDIRECTS['/drift']).toBe('/operate/drift')
    expect(ROUTE_REDIRECTS['/governance']).toBe('/govern/governance')
    expect(ROUTE_REDIRECTS['/status']).toBe('/operate/self-obs')
    expect(ROUTE_REDIRECTS['/next-gen']).toBe('/serve/nextgen')
  })
})

describe('isNavItemActive', () => {
  it('matches "/" only exactly', () => {
    expect(isNavItemActive('/', '/')).toBe(true)
    expect(isNavItemActive('/', '/build/models')).toBe(false)
  })

  it('matches a leaf route and its detail children, not sibling prefixes', () => {
    expect(isNavItemActive('/build/models', '/build/models')).toBe(true)
    expect(isNavItemActive('/build/models', '/build/models/JPCP')).toBe(true)
    expect(isNavItemActive('/build/models', '/build/modelspecial')).toBe(false)
  })
})

describe('activeSectionId', () => {
  it('resolves the owning group for a route', () => {
    expect(activeSectionId('/operate/drift')).toBe('operate')
    expect(activeSectionId('/govern/audit')).toBe('govern')
    expect(activeSectionId('/build/models/JPCP')).toBe('build')
    expect(activeSectionId('/platform/jupyter')).toBe('platform')
  })

  it('returns null for Home, utility and unknown routes', () => {
    expect(activeSectionId('/')).toBeNull()
    expect(activeSectionId('/documents')).toBeNull()
    expect(activeSectionId('/nope')).toBeNull()
  })
})

describe('pageTitleForPath', () => {
  it('uses canonical navigation labels for list and utility routes', () => {
    expect(pageTitleForPath('/')).toBe('Overview')
    expect(pageTitleForPath('/operate/self-obs')).toBe('Self-Obs')
    expect(pageTitleForPath('/preferences')).toBe('Preferences')
  })

  it('adds the decoded entity name for a detail route', () => {
    expect(pageTitleForPath('/build/models/forecast%20v2')).toBe('forecast v2 · Models')
  })

  it('labels special and unknown routes honestly', () => {
    expect(pageTitleForPath('/noc')).toBe('NOC wall')
    expect(pageTitleForPath('/does-not-exist')).toBe('Page not found')
  })
})
