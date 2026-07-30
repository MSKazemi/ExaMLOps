/**
 * Grouped navigation model (dashboard-enterprise-rebuild M0, BL-013a).
 *
 * Single source of truth for the sidebar. The 23 flat tabs are re-parented into the six
 * lifecycle groups from `01-information-architecture.md` (Home · Build · Serve · Operate ·
 * Govern · Platform) so the shell reads as an enterprise control plane instead of a flat list.
 *
 * URLs are lifecycle-scoped (`/build/models`, `/operate/drift`, …; ADR 0097 §3, BL-013c). Old flat
 * paths (`/models`) redirect here via `ROUTE_REDIRECTS` for one release. Adding a group or moving an
 * item = edit this file; `Layout`, the router redirects, and the nav test all derive from it.
 */
import {
  Activity,
  BellRing,
  BookOpen,
  Bot,
  Box,
  Boxes,
  ClipboardCheck,
  Cpu,
  Database,
  DollarSign,
  FileCheck,
  Flag,
  FolderKanban,
  GaugeCircle,
  GitBranch,
  KeyRound,
  Layers,
  LayoutDashboard,
  Lock,
  NotebookPen,
  Rocket,
  Scale,
  ScrollText,
  Server,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Target,
  Zap,
} from 'lucide-react'

export type NavIcon = typeof LayoutDashboard

export interface NavItem {
  path: string
  label: string
  icon: NavIcon
  /** Admin-only items are hidden from viewers (mirrors the route guard). */
  adminOnly?: boolean
  /** Feature-flag key: the item is hidden unless the flag is enabled (matches App.tsx routing). */
  flag?: string
  /** Named live badge to render next to the item (currently only the pending-approvals count). */
  badge?: 'approvals'
}

export interface NavSection {
  id: string
  label: string
  icon: NavIcon
  items: NavItem[]
}

/** The command-center landing, rendered above the collapsible groups. */
export const HOME_ITEM: NavItem = { path: '/', label: 'Overview', icon: LayoutDashboard }

/** The six lifecycle groups (Home is `HOME_ITEM`; utility links are `UTILITY_NAV`). */
export const NAV_SECTIONS: NavSection[] = [
  {
    id: 'build',
    label: 'Build',
    icon: Box,
    items: [
      { path: '/build/models', label: 'Models', icon: Box },
      { path: '/build/mlops', label: 'MLOps', icon: Boxes, flag: 'mlopsConsole' },
      { path: '/build/datasets', label: 'Datasets', icon: Database },
      { path: '/build/features', label: 'Features', icon: Layers },
      { path: '/build/pipelines', label: 'Pipelines', icon: GitBranch },
      { path: '/build/prompts', label: 'Prompts', icon: ScrollText },
    ],
  },
  {
    id: 'serve',
    label: 'Serve',
    icon: Server,
    items: [
      { path: '/serve/llmops', label: 'LLMOps', icon: Bot },
      { path: '/serve/gateway', label: 'Gateway', icon: KeyRound },
      { path: '/serve/nextgen', label: 'Next-Gen', icon: Sparkles },
    ],
  },
  {
    id: 'operate',
    label: 'Operate',
    icon: Activity,
    items: [
      { path: '/operate/drift', label: 'Drift', icon: Activity },
      { path: '/operate/alerts', label: 'Alerts', icon: BellRing },
      { path: '/operate/autopilot', label: 'Autopilot', icon: Rocket },
      { path: '/operate/slos', label: 'SLOs', icon: Target },
      { path: '/operate/facility', label: 'Facility', icon: Cpu, flag: 'facilityConsole' },
      { path: '/operate/finops', label: 'FinOps', icon: DollarSign },
      { path: '/operate/self-obs', label: 'Self-Obs', icon: GaugeCircle },
    ],
  },
  {
    id: 'govern',
    label: 'Govern',
    icon: ShieldCheck,
    items: [
      { path: '/govern/compliance', label: 'Compliance', icon: FileCheck, adminOnly: true },
      { path: '/govern/audit', label: 'Audit', icon: ShieldCheck, adminOnly: true },
      { path: '/govern/approvals', label: 'Approvals', icon: ClipboardCheck, adminOnly: true, badge: 'approvals' },
      { path: '/govern/fairness', label: 'Fairness', icon: Scale, adminOnly: true },
      { path: '/govern/secrets', label: 'Secrets', icon: Lock, adminOnly: true },
    ],
  },
  {
    id: 'platform',
    label: 'Platform',
    icon: FolderKanban,
    items: [
      { path: '/platform/projects', label: 'Projects', icon: FolderKanban, flag: 'projectsConsole' },
      { path: '/platform/services', label: 'Services', icon: Server },
      { path: '/platform/config', label: 'Config', icon: Settings2 },
      { path: '/platform/integrations', label: 'SeanerBUS', icon: Zap },
      { path: '/platform/jupyter', label: 'Jupyter', icon: NotebookPen },
      { path: '/platform/flags', label: 'Flags', icon: Flag, adminOnly: true },
    ],
  },
]

/** Utility links, pinned to the sidebar footer (not a lifecycle group). */
export const UTILITY_NAV: NavItem[] = [
  { path: '/documents', label: 'Documents', icon: BookOpen },
  { path: '/preferences', label: 'Preferences', icon: SlidersHorizontal },
]

/**
 * Old flat path → new lifecycle-scoped path (ADR 0097 §3). The router renders a client 301
 * (`<Navigate replace>`) from each old base (and any sub-path) so bookmarks, the command palette,
 * persisted landing prefs, and external links keep working for one release. One source of truth for
 * the migration — the router derives its redirect routes from this map.
 */
export const ROUTE_REDIRECTS: Record<string, string> = {
  '/models': '/build/models',
  '/mlops': '/build/mlops',
  '/datasets': '/build/datasets',
  '/pipelines': '/build/pipelines',
  '/llmops': '/serve/llmops',
  '/nextgen': '/serve/nextgen',
  '/next-gen': '/serve/nextgen',
  '/drift': '/operate/drift',
  '/alerts': '/operate/alerts',
  '/facility': '/operate/facility',
  '/finops': '/operate/finops',
  '/status': '/operate/self-obs',
  '/governance': '/govern/compliance',
  '/audit': '/govern/audit',
  '/approvals': '/govern/approvals',
  '/projects': '/platform/projects',
  '/services': '/platform/services',
  '/config': '/platform/config',
  '/seanerbus': '/platform/integrations',
  '/jupyter': '/platform/jupyter',
}

/** True when `itemPath` is the active route (exact for '/', prefix-aware for detail pages). */
export function isNavItemActive(itemPath: string, pathname: string): boolean {
  if (itemPath === '/') return pathname === '/'
  return pathname === itemPath || pathname.startsWith(itemPath + '/')
}

/** The id of the section that owns the active route, or null (Home/utility/unknown). */
export function activeSectionId(pathname: string): string | null {
  for (const section of NAV_SECTIONS) {
    if (section.items.some((it) => isNavItemActive(it.path, pathname))) return section.id
  }
  return null
}
