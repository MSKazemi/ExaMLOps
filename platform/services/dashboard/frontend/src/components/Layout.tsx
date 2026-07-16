import { Link, useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils'
import {
  LayoutDashboard,
  Server,
  Box,
  Database,
  Settings2,
  SlidersHorizontal,
  BookOpen,
  Zap,
  ShieldCheck,
  ClipboardCheck,
  GitBranch,
  NotebookPen,
  Activity,
  Boxes,
  Cpu,
  FolderKanban,
  DollarSign,
  FileCheck,
  Bot,
  BellRing,
  Flag,
  Sparkles,
  LogOut,
  Sun,
  Moon,
  MoonStar,
} from 'lucide-react'
import uniboLogo from '@/assets/unibo.png'
import seanergysLogo from '@/assets/seanergys.jpg'
import { clearAuth, getRole } from '@/lib/auth'
import { useCapabilities } from '@/lib/capabilities'
import { useTheme, type Theme } from '@/lib/theme'
import { useApprovalsCount } from '@/lib/api'
import { CommandPalette } from '@/components/CommandPalette'
import { CopilotPanel } from '@/components/CopilotPanel'
import { HelpDrawer } from '@/components/HelpDrawer'
import { OnboardingTour } from '@/components/OnboardingTour'
import { SkipLink } from '@/components/SkipLink'
import { LocaleSwitcher } from '@/components/LocaleSwitcher'

const BASE_NAV = [
  { path: '/',          label: 'Overview', icon: LayoutDashboard, adminOnly: false },
  { path: '/services',  label: 'Services', icon: Server,          adminOnly: false },
  { path: '/models',    label: 'Models',   icon: Box,             adminOnly: false },
  { path: '/projects',  label: 'Projects', icon: FolderKanban,    adminOnly: false },
  { path: '/mlops',     label: 'MLOps',    icon: Boxes,           adminOnly: false },
  { path: '/llmops',    label: 'LLMOps',   icon: Bot,             adminOnly: false },
  { path: '/nextgen',   label: 'Next-Gen', icon: Sparkles,        adminOnly: false },
  { path: '/datasets',   label: 'Datasets',  icon: Database,   adminOnly: false },
  { path: '/pipelines', label: 'Pipelines', icon: GitBranch,     adminOnly: false },
  { path: '/facility',  label: 'Facility',  icon: Cpu,           adminOnly: false },
  { path: '/finops',    label: 'FinOps',    icon: DollarSign,    adminOnly: false },
  { path: '/jupyter',   label: 'Jupyter',   icon: NotebookPen,   adminOnly: false },
  { path: '/seanerbus', label: 'SeanerBUS', icon: Zap,           adminOnly: false },
  { path: '/drift',     label: 'Drift',     icon: Activity,      adminOnly: false },
  { path: '/alerts',    label: 'Alerts',    icon: BellRing,      adminOnly: false },
  { path: '/preferences', label: 'Preferences', icon: SlidersHorizontal, adminOnly: false },
  { path: '/config',    label: 'Config',    icon: Settings2,  adminOnly: false },
  { path: '/audit',     label: 'Audit',    icon: ShieldCheck,     adminOnly: true  },
  { path: '/governance', label: 'Governance', icon: FileCheck,   adminOnly: true  },
  { path: '/flags',     label: 'Flags',     icon: Flag,          adminOnly: true  },
  { path: '/approvals', label: 'Approvals', icon: ClipboardCheck, adminOnly: true  },
  { path: '/status',    label: 'Status',    icon: Activity,      adminOnly: false },
  { path: '/docs',      label: 'Docs',     icon: BookOpen,        adminOnly: false },
] as const

const THEMES: { value: Theme; icon: typeof Sun; label: string }[] = [
  { value: 'day',      icon: Sun,      label: 'Day'      },
  { value: 'night',    icon: Moon,     label: 'Night'    },
  { value: 'midnight', icon: MoonStar, label: 'Midnight' },
]

export function Layout({ children }: { children: React.ReactNode }) {
  const { pathname } = useLocation()
  const role = getRole()
  const { tenant } = useCapabilities()
  const nav = BASE_NAV.filter(n => !n.adminOnly || role === 'admin')
  const { theme, setTheme } = useTheme()
  const { data: pendingApprovals } = useApprovalsCount()
  const pendingCount = pendingApprovals?.length ?? 0

  const handleSignOut = () => {
    clearAuth()
    window.location.reload()
  }

  return (
    <div className="h-screen flex bg-background text-foreground overflow-hidden">
      <SkipLink />
      <CommandPalette />
      <aside
        className="no-print w-56 shrink-0 flex flex-col border-r border-border"
        style={{ background: 'var(--sidebar)' }}
      >
        <div className="p-4 border-b border-border">
          <div className="flex items-center gap-2.5 mb-4">
            <div className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0"
              style={{
                background: 'oklch(0.64 0.20 265 / 18%)',
                border: '1px solid oklch(0.64 0.20 265 / 35%)',
                boxShadow: '0 0 12px oklch(0.64 0.20 265 / 20%)',
              }}
            >
              <Zap className="w-4 h-4" style={{ color: 'var(--accent-text)' }} />
            </div>
            <div>
              <p className="font-bold text-sm leading-none">ExaMLOps</p>
              <p className="text-[10px] text-muted-foreground mt-0.5 tracking-wide">MLOps Platform</p>
            </div>
          </div>
          <div className="flex items-center gap-2.5 px-0.5">
            <img src={uniboLogo} alt="University of Bologna" className="h-7 w-7 object-contain opacity-75" />
            <div className="h-4 w-px bg-border" />
            <img src={seanergysLogo} alt="SEANERGYS" className="h-6 object-contain max-w-[90px] opacity-75" />
          </div>
        </div>

        <nav className="flex-1 p-2.5 space-y-0.5">
          {nav.map(({ path, label, icon: Icon }) => {
            const active = pathname === path
            const showBadge = path === '/approvals' && pendingCount > 0
            return (
              <Link
                key={path}
                to={path}
                className={cn(
                  'flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm font-medium transition-all duration-150',
                  active
                    ? 'text-white'
                    : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
                )}
                style={active ? {
                  background: 'oklch(0.64 0.20 265 / 18%)',
                  border: '1px solid oklch(0.64 0.20 265 / 25%)',
                  color: 'var(--accent-text)',
                } : undefined}
              >
                <Icon className="w-4 h-4 shrink-0" />
                <span className="flex-1">{label}</span>
                {showBadge && (
                  <span
                    className="ml-auto inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full text-[10px] font-bold leading-none"
                    style={{
                      background: 'oklch(0.78 0.18 55 / 85%)',
                      color: 'oklch(0.20 0.05 55)',
                    }}
                  >
                    {pendingCount > 99 ? '99+' : pendingCount}
                  </span>
                )}
              </Link>
            )
          })}
        </nav>

        <div className="p-3 border-t border-border space-y-2">
          {/* Theme toggle */}
          <div className="flex items-center gap-1 rounded-lg p-0.5" style={{ background: 'var(--surface-1)', border: '1px solid var(--border)' }}>
            {THEMES.map(({ value, icon: Icon, label }) => (
              <button
                key={value}
                onClick={() => setTheme(value)}
                title={label}
                aria-label={`${label} theme`}
                className={cn(
                  'flex-1 flex items-center justify-center py-1 rounded-md transition-all duration-150',
                  theme === value ? 'text-white' : 'text-muted-foreground hover:text-foreground'
                )}
                style={theme === value ? {
                  background: 'oklch(0.64 0.20 265 / 22%)',
                  boxShadow: '0 0 8px oklch(0.64 0.20 265 / 20%)',
                } : undefined}
              >
                <Icon className="w-3.5 h-3.5" />
              </button>
            ))}
          </div>

          {/* Locale switcher (F19) */}
          <div className="flex items-center justify-center">
            <LocaleSwitcher />
          </div>

          {role && (
            <div className="flex items-center justify-between text-[11px]">
              <span
                className="px-2 py-0.5 rounded-md font-medium uppercase tracking-wide"
                style={{
                  background: role === 'admin'
                    ? 'oklch(0.78 0.18 55 / 12%)'
                    : 'oklch(0.72 0.18 155 / 12%)',
                  border: role === 'admin'
                    ? '1px solid oklch(0.78 0.18 55 / 30%)'
                    : '1px solid oklch(0.72 0.18 155 / 30%)',
                  color: role === 'admin' ? 'var(--warning-text)' : 'var(--success-text)',
                }}
              >
                {role}
              </span>
              {/* Tenant indicator (F15 R4) — shown once a non-default tenant is in scope. */}
              {tenant && tenant !== 'default' && (
                <span
                  className="px-2 py-0.5 rounded-md font-medium tracking-wide text-muted-foreground"
                  style={{ background: 'var(--surface-2)', border: '1px solid var(--border)' }}
                  title="Active tenant"
                >
                  {tenant}
                </span>
              )}
              <button
                onClick={handleSignOut}
                className="inline-flex items-center gap-1 text-muted-foreground hover:text-foreground"
                aria-label="Sign out"
              >
                <LogOut className="w-3 h-3" /> sign out
              </button>
            </div>
          )}
          <p className="text-[10px] text-muted-foreground/50 tracking-wide">
            SEANERGYS · EuroHPC-JU
          </p>
        </div>
      </aside>

      <main id="main" tabIndex={-1} className="flex-1 min-h-0 overflow-auto">{children}</main>
      <CopilotPanel />
      <HelpDrawer />
      <OnboardingTour />
    </div>
  )
}
