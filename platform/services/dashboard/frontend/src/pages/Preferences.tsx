import { Link } from 'react-router-dom'
import { SlidersHorizontal, Star } from 'lucide-react'
import { EmptyState } from '@/components/ui/empty-state'
import { PinButton } from '@/components/PinButton'
import { LocaleSwitcher } from '@/components/LocaleSwitcher'
import { usePrefs, useWatchlist, useOnboarding } from '@/lib/prefs'

// Landing pages a user can pick as their default (R3).
const LANDING_OPTIONS = [
  { value: '/', label: 'Overview' },
  { value: '/models', label: 'Models' },
  { value: '/pipelines', label: 'Pipelines' },
  { value: '/drift', label: 'Drift' },
  { value: '/finops', label: 'FinOps' },
  { value: '/alerts', label: 'Alerts' },
]

/** Preference center + watchlist (F21 / ADR 0072, R3/R4). */
export function Preferences() {
  const { prefs, setPref } = usePrefs()
  const { pinned } = useWatchlist()
  const { reset } = useOnboarding()

  return (
    <div className="p-6 space-y-6 max-w-3xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <SlidersHorizontal className="size-6 text-muted-foreground" aria-hidden="true" />
          Preferences
        </h1>
        <p className="text-sm text-muted-foreground">
          Personal settings, saved to this browser. Theme and status live in the sidebar.
        </p>
      </div>

      <section className="space-y-3">
        <h2 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">General</h2>
        <div className="flex items-center justify-between rounded-lg border border-border px-4 py-3 text-sm">
          <label htmlFor="landing">Default landing page</label>
          <select
            id="landing"
            value={prefs.defaultLanding}
            onChange={(e) => setPref('defaultLanding', e.target.value)}
            className="rounded-md border border-border bg-transparent px-2 py-1 text-sm"
          >
            {LANDING_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex items-center justify-between rounded-lg border border-border px-4 py-3 text-sm">
          <label htmlFor="density">Density</label>
          <select
            id="density"
            value={prefs.density}
            onChange={(e) => setPref('density', e.target.value as 'comfortable' | 'compact')}
            className="rounded-md border border-border bg-transparent px-2 py-1 text-sm"
          >
            <option value="comfortable">Comfortable</option>
            <option value="compact">Compact</option>
          </select>
        </div>
        <div className="flex items-center justify-between rounded-lg border border-border px-4 py-3 text-sm">
          <span>Language</span>
          <LocaleSwitcher />
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">Watchlist</h2>
        {pinned.length === 0 ? (
          <EmptyState
            title="No pinned entities"
            description="Pin models or jobs with the star icon to follow them here."
          />
        ) : (
          <ul className="space-y-1">
            {pinned.map((r) => (
              <li key={`${r.type}:${r.id}`} className="flex items-center justify-between rounded-lg border border-border px-4 py-2 text-sm">
                <Link to={`/${r.type}/${r.id}`} className="flex items-center gap-2 hover:underline">
                  <Star className="size-3.5 fill-current text-amber-500" aria-hidden="true" />
                  <span className="font-medium">{r.id}</span>
                  <span className="text-xs text-muted-foreground">{r.type}</span>
                </Link>
                <PinButton entity={r} label={r.id} />
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-3">
        <h2 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">Onboarding</h2>
        <button
          type="button"
          onClick={reset}
          className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted"
        >
          Replay the getting-started tour
        </button>
      </section>
    </div>
  )
}
