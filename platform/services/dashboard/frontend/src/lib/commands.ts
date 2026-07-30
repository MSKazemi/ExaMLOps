import type { Role } from './auth'
import { HOME_ITEM, NAV_SECTIONS, UTILITY_NAV, type NavItem } from './nav'

// ── command registry (F2 R1/R2) ──────────────────────────────────────────────

export interface Command {
  id: string
  label: string
  /** Navigation target (nav commands). */
  to?: string
  /** Roles allowed to see/run this command; omitted ⇒ everyone. */
  scopes?: Role[]
  /** Grouping label in the palette. */
  group: 'Navigate' | 'Actions'
  /** GUI↔CLI parity (F2 R6): the equivalent `exa` command, if any. */
  cliEquivalent?: string
}

/** Stable command id from a route (last path segment) — `/govern/audit` → `nav-audit`. */
function navId(path: string): string {
  return `nav-${path.split('/').filter(Boolean).pop() ?? 'overview'}`
}

/**
 * Navigate commands are DERIVED from the sidebar nav (single source of truth: `lib/nav.ts`), so
 * every console — including new ones — is searchable via ⌘K and the palette can never drift from the
 * nav. Admin-only nav items become admin-scoped commands (F15).
 */
const NAV_COMMANDS: Command[] = [
  HOME_ITEM,
  ...NAV_SECTIONS.flatMap((s) => s.items),
  ...UTILITY_NAV,
].map((item: NavItem) => ({
  id: navId(item.path),
  label: `Go to ${item.label}`,
  to: item.path,
  group: 'Navigate' as const,
  ...(item.adminOnly ? { scopes: ['admin'] as Role[] } : {}),
}))

// Actions carry an `exa` equivalent for GUI↔CLI parity; the run/confirm gate lives in the page.
const ACTION_COMMANDS: Command[] = [
  { id: 'act-status', label: 'Copy: platform status command', group: 'Actions', cliEquivalent: 'exa status' },
  { id: 'act-drift', label: 'Copy: drift status command', group: 'Actions', cliEquivalent: 'exa drift status' },
  { id: 'act-projects-list', label: 'Copy: list projects command', group: 'Actions', cliEquivalent: 'exa project list' },
  {
    id: 'act-project-create', label: 'Copy: create project command', scopes: ['admin'], group: 'Actions',
    cliEquivalent: 'exa project create <name>',
  },
]

/** All navigation + action commands. Authorization (F15) is applied by {@link visibleCommands}. */
export const COMMANDS: Command[] = [...NAV_COMMANDS, ...ACTION_COMMANDS]

/** Commands visible to a role — unauthorized ones are hidden (F2 R2 / F15). */
export function visibleCommands(role: Role | null): Command[] {
  return COMMANDS.filter((c) => !c.scopes || (role && c.scopes.includes(role)))
}
