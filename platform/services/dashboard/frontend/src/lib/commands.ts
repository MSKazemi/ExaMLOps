import type { Role } from './auth'

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

/** All navigation + action commands. Authorization (F15) is applied by {@link visibleCommands}. */
export const COMMANDS: Command[] = [
  { id: 'nav-overview', label: 'Go to Overview', to: '/', group: 'Navigate' },
  { id: 'nav-services', label: 'Go to Services', to: '/services', group: 'Navigate' },
  { id: 'nav-models', label: 'Go to Models', to: '/models', group: 'Navigate' },
  { id: 'nav-projects', label: 'Go to Projects', to: '/projects', group: 'Navigate' },
  { id: 'nav-mlops', label: 'Go to MLOps Console', to: '/mlops', group: 'Navigate' },
  { id: 'nav-facility', label: 'Go to Facility Console', to: '/facility', group: 'Navigate' },
  { id: 'nav-datasets', label: 'Go to Datasets', to: '/datasets', group: 'Navigate' },
  { id: 'nav-pipelines', label: 'Go to Pipelines', to: '/pipelines', group: 'Navigate' },
  { id: 'nav-drift', label: 'Go to Drift', to: '/drift', group: 'Navigate' },
  { id: 'nav-approvals', label: 'Go to Approvals', to: '/approvals', scopes: ['admin'], group: 'Navigate' },
  { id: 'nav-audit', label: 'Go to Audit', to: '/audit', scopes: ['admin'], group: 'Navigate' },
  { id: 'nav-config', label: 'Go to Config', to: '/config', group: 'Navigate' },
  { id: 'nav-docs', label: 'Go to Docs', to: '/docs', group: 'Navigate' },
  // Actions carry an `exa` equivalent for GUI↔CLI parity; the run/confirm gate lives in the page.
  {
    id: 'act-status', label: 'Copy: platform status command', group: 'Actions',
    cliEquivalent: 'exa status',
  },
  {
    id: 'act-drift', label: 'Copy: drift status command', group: 'Actions',
    cliEquivalent: 'exa drift status',
  },
  {
    id: 'act-projects-list', label: 'Copy: list projects command', group: 'Actions',
    cliEquivalent: 'exa project list',
  },
  {
    id: 'act-project-create', label: 'Copy: create project command', scopes: ['admin'], group: 'Actions',
    cliEquivalent: 'exa project create <name>',
  },
]

/** Commands visible to a role — unauthorized ones are hidden (F2 R2 / F15). */
export function visibleCommands(role: Role | null): Command[] {
  return COMMANDS.filter((c) => !c.scopes || (role && c.scopes.includes(role)))
}
