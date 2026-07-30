import { useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { ChevronDown, Zap, LogOut, Sun, Moon, MoonStar } from 'lucide-react'
import uniboLogo from '@/assets/unibo.png'
import seanergysLogo from '@/assets/seanergys.jpg'
import { clearAuth, getRole } from '@/lib/auth'
import { useCapabilities } from '@/lib/capabilities'
import { useTheme, type Theme } from '@/lib/theme'
import { useApprovalsCount } from '@/lib/api'
import { isEnabled } from '@/lib/flags'
import {
  HOME_ITEM,
  NAV_SECTIONS,
  UTILITY_NAV,
  activeSectionId,
  isNavItemActive,
  type NavItem,
} from '@/lib/nav'
import { CommandPalette } from '@/components/CommandPalette'
import { CopilotPanel } from '@/components/CopilotPanel'
import { HelpDrawer } from '@/components/HelpDrawer'
import { OnboardingTour } from '@/components/OnboardingTour'
import { SkipLink } from '@/components/SkipLink'
import { LocaleSwitcher } from '@/components/LocaleSwitcher'

const THEMES: { value: Theme; icon: typeof Sun; label: string }[] = [
  { value: 'day',      icon: Sun,      label: 'Day'      },
  { value: 'night',    icon: Moon,     label: 'Night'    },
  { value: 'midnight', icon: MoonStar, label: 'Midnight' },
]

// v2: bumped so the new "all groups collapsed by default" applies for everyone, ignoring any
// stale all-expanded preference saved under the old key.
const COLLAPSED_KEY = 'exa.nav.collapsed.v2'

function readCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY)
    if (raw) return new Set<string>(JSON.parse(raw))
  } catch {
    /* storage unavailable (private mode) — fall through to the default */
  }
  // First visit (no saved preference): collapse EVERY group so the sidebar stays compact
  // instead of showing all ~30 items at once. The active group is force-opened in render
  // (`open = !collapsed.has(id) || openSection === id`), so the current section is always visible.
  return new Set<string>(NAV_SECTIONS.map((s) => s.id))
}

export function Layout({ children }: { children: React.ReactNode }) {
  const { pathname } = useLocation()
  const role = getRole()
  const { tenant } = useCapabilities()
  const { theme, setTheme } = useTheme()
  const { data: pendingApprovals } = useApprovalsCount()
  const pendingCount = pendingApprovals?.length ?? 0
  const [collapsed, setCollapsed] = useState<Set<string>>(readCollapsed)
  const openSection = activeSectionId(pathname)

  // An item is shown when the role clears any admin gate and its feature flag (if any) is on.
  const canSee = (item: NavItem) =>
    (!item.adminOnly || role === 'admin') &&
    (!item.flag || isEnabled(item.flag as Parameters<typeof isEnabled>[0]))

  const toggleSection = (id: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      try {
        localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...next]))
      } catch {
        /* storage unavailable (private mode) — collapse state stays in-memory only */
      }
      return next
    })

  const handleSignOut = () => {
    clearAuth()
    window.location.reload()
  }

  // Rendered for both the Home link, section items, and utility links (one consistent row).
  const renderLink = (item: NavItem) => {
    const active = isNavItemActive(item.path, pathname)
    const showBadge = item.badge === 'approvals' && pendingCount > 0
    const Icon = item.icon
    return (
      <Link
        key={item.path}
        to={item.path}
        aria-current={active ? 'page' : undefined}
        className={cn(
          'flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm font-medium transition-all duration-150',
          active
            ? 'text-white'
            : 'text-muted-foreground hover:text-foreground hover:bg-accent/60',
        )}
        style={active ? {
          background: 'oklch(0.64 0.20 265 / 18%)',
          border: '1px solid oklch(0.64 0.20 265 / 25%)',
          color: 'var(--accent-text)',
        } : undefined}
      >
        <Icon className="w-4 h-4 shrink-0" />
        <span className="flex-1">{item.label}</span>
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

        <nav className="flex-1 p-2.5 space-y-1 overflow-y-auto" aria-label="Primary">
          {/* Home / command center */}
          {renderLink(HOME_ITEM)}

          {/* Six lifecycle groups (collapsible; the active group is always open). */}
          {NAV_SECTIONS.map((section) => {
            const items = section.items.filter(canSee)
            if (items.length === 0) return null
            const SectionIcon = section.icon
            const open = !collapsed.has(section.id) || openSection === section.id
            const sectionHasPending =
              section.id === 'govern' && pendingCount > 0 && items.some((i) => i.badge === 'approvals')
            return (
              <div key={section.id} className="pt-1.5">
                <button
                  type="button"
                  onClick={() => toggleSection(section.id)}
                  aria-expanded={open}
                  className="w-full flex items-center gap-2 px-3 py-1 rounded-md text-[11px] font-semibold uppercase tracking-wider text-muted-foreground/70 hover:text-foreground transition-colors"
                >
                  <SectionIcon className="w-3.5 h-3.5 shrink-0 opacity-70" />
                  <span className="flex-1 text-left">{section.label}</span>
                  {sectionHasPending && !open && (
                    <span
                      className="inline-flex items-center justify-center min-w-[16px] h-[16px] px-1 rounded-full text-[9px] font-bold leading-none"
                      style={{ background: 'oklch(0.78 0.18 55 / 85%)', color: 'oklch(0.20 0.05 55)' }}
                    >
                      {pendingCount > 99 ? '99+' : pendingCount}
                    </span>
                  )}
                  <ChevronDown
                    className={cn('w-3.5 h-3.5 shrink-0 transition-transform duration-150', open ? '' : '-rotate-90')}
                  />
                </button>
                {open && <div className="mt-0.5 space-y-0.5">{items.map(renderLink)}</div>}
              </div>
            )
          })}

          {/* Utility links (Docs, Preferences) */}
          <div className="pt-2 mt-1 border-t border-border/60 space-y-0.5">
            {UTILITY_NAV.filter(canSee).map(renderLink)}
          </div>
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
