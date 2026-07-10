import { useCallback, useState } from 'react'

// Per-user personalization store (F21 / ADR 0072) — preferences, watchlist, recents, and onboarding
// state persisted in localStorage. Pure list helpers + storage are unit-tested; hooks are thin wrappers.
// (This slice persists locally; graduating to the BFF UI-state store — R1 cross-device — is deferred.)

const STORAGE_KEY = 'dashboard.prefs.v1'

export type Density = 'comfortable' | 'compact'

export interface Prefs {
  defaultLanding: string
  density: Density
}

export const DEFAULT_PREFS: Prefs = { defaultLanding: '/', density: 'comfortable' }

export interface EntityRef {
  type: string
  id: string
}

interface PrefStore {
  prefs: Prefs
  watchlist: EntityRef[]
  recents: EntityRef[]
  tourDone: boolean
}

const EMPTY: PrefStore = { prefs: DEFAULT_PREFS, watchlist: [], recents: [], tourDone: false }

// ── pure list helpers ─────────────────────────────────────────────────────────

export function refKey(r: EntityRef): string {
  return `${r.type}:${r.id}`
}

/** Toggle an entity's presence in a watchlist (immutable). */
export function togglePinInList(list: EntityRef[], r: EntityRef): EntityRef[] {
  return list.some((x) => refKey(x) === refKey(r)) ? list.filter((x) => refKey(x) !== refKey(r)) : [...list, r]
}

/** Prepend to a recents list, de-duplicated and capped (most-recent-first). */
export function pushRecentInList(list: EntityRef[], r: EntityRef, max = 8): EntityRef[] {
  return [r, ...list.filter((x) => refKey(x) !== refKey(r))].slice(0, max)
}

// ── storage (guarded) ─────────────────────────────────────────────────────────

function read(): PrefStore {
  if (typeof localStorage === 'undefined') return EMPTY
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return EMPTY
    const parsed = JSON.parse(raw)
    return {
      prefs: { ...DEFAULT_PREFS, ...(parsed.prefs ?? {}) },
      watchlist: Array.isArray(parsed.watchlist) ? parsed.watchlist : [],
      recents: Array.isArray(parsed.recents) ? parsed.recents : [],
      tourDone: Boolean(parsed.tourDone),
    }
  } catch {
    return EMPTY
  }
}

function write(patch: Partial<PrefStore>): PrefStore {
  const next = { ...read(), ...patch }
  if (typeof localStorage !== 'undefined') localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
  return next
}

// ── hooks ─────────────────────────────────────────────────────────────────────

/** User preferences (default landing page + density), persisted per browser (R3). */
export function usePrefs(): { prefs: Prefs; setPref: <K extends keyof Prefs>(key: K, value: Prefs[K]) => void } {
  const [prefs, setPrefs] = useState<Prefs>(() => read().prefs)
  const setPref = useCallback(<K extends keyof Prefs>(key: K, value: Prefs[K]) => {
    setPrefs((prev) => {
      const next = { ...prev, [key]: value }
      write({ prefs: next })
      return next
    })
  }, [])
  return { prefs, setPref }
}

/** Watchlist / favourites — pin entities to follow (R4). */
export function useWatchlist(): {
  pinned: EntityRef[]
  isPinned: (r: EntityRef) => boolean
  toggle: (r: EntityRef) => void
} {
  const [pinned, setPinned] = useState<EntityRef[]>(() => read().watchlist)
  const toggle = useCallback((r: EntityRef) => {
    setPinned((prev) => {
      const next = togglePinInList(prev, r)
      write({ watchlist: next })
      return next
    })
  }, [])
  const isPinned = useCallback((r: EntityRef) => pinned.some((x) => refKey(x) === refKey(r)), [pinned])
  return { pinned, isPinned, toggle }
}

/** First-run onboarding state — the tour runs once until completed/skipped (R5/GWT-5). */
export function useOnboarding(): { done: boolean; complete: () => void; reset: () => void } {
  const [done, setDone] = useState<boolean>(() => read().tourDone)
  const complete = useCallback(() => {
    write({ tourDone: true })
    setDone(true)
  }, [])
  const reset = useCallback(() => {
    write({ tourDone: false })
    setDone(false)
  }, [])
  return { done, complete, reset }
}
