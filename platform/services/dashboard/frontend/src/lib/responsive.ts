import { useEffect, useState } from 'react'

// Responsive / multi-device helpers (F20 / ADR 0069). Pure logic + small hooks, dependency-free. The
// NOC-wall rotation and kiosk detection live here so they are unit-tested without a DOM.

export type Breakpoint = 'laptop' | 'desktop' | 'wide' | 'ultrawide'

// Min-width thresholds (px). Laptop is the floor; the shell + F17/F4 grids reflow across these (R1).
export const BREAKPOINTS: { name: Breakpoint; min: number }[] = [
  { name: 'ultrawide', min: 2560 },
  { name: 'wide', min: 1920 },
  { name: 'desktop', min: 1280 },
  { name: 'laptop', min: 0 },
]

/** Classify a viewport width into a breakpoint (R1). */
export function matchBreakpoint(width: number): Breakpoint {
  return (BREAKPOINTS.find((b) => width >= b.min) ?? BREAKPOINTS[BREAKPOINTS.length - 1]).name
}

/** True when the URL requests NOC/kiosk mode (`?kiosk=1` / `?kiosk=true`). */
export function isKioskMode(search: string): boolean {
  const v = new URLSearchParams(search).get('kiosk')
  return v === '1' || v === 'true'
}

/** Next index in a wrap-around rotation (NOC auto-cycle, R2). */
export function nextRotationIndex(current: number, length: number): number {
  if (length <= 0) return 0
  return (current + 1) % length
}

/** `window.open` feature string for a detached live panel (R5). */
export function popOutFeatures(width = 720, height = 540): string {
  return `popup,width=${width},height=${height},noopener,noreferrer`
}

/** Open a URL in a detached window (pop-out live panel, R5). Returns false if blocked/unavailable. */
export function popOut(url: string, name = 'examlops-panel'): boolean {
  if (typeof window === 'undefined' || typeof window.open !== 'function') return false
  const win = window.open(url, name, popOutFeatures())
  return win != null
}

// ── hooks ─────────────────────────────────────────────────────────────────────

/** Reactively evaluate a media query (SSR/jsdom-safe). */
export function useMediaQuery(query: string): boolean {
  // Seed from the current match in the initializer (no setState in the effect body) so React-19's
  // set-state-in-effect rule stays satisfied; the effect only subscribes to changes.
  const [matches, setMatches] = useState(() =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia(query).matches
      : false,
  )
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mql = window.matchMedia(query)
    const handler = (e: MediaQueryListEvent) => setMatches(e.matches)
    mql.addEventListener('change', handler)
    return () => mql.removeEventListener('change', handler)
  }, [query])
  return matches
}

/** Current viewport breakpoint, updated on resize (R1). */
export function useBreakpoint(): Breakpoint {
  const [bp, setBp] = useState<Breakpoint>(() =>
    typeof window === 'undefined' ? 'desktop' : matchBreakpoint(window.innerWidth),
  )
  useEffect(() => {
    if (typeof window === 'undefined') return
    const onResize = () => setBp(matchBreakpoint(window.innerWidth))
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  return bp
}

/** Auto-advancing index for a rotation of `length` items every `intervalMs` (NOC wall, R2). */
export function useRotator(length: number, intervalMs: number, enabled = true): number {
  const [index, setIndex] = useState(0)
  useEffect(() => {
    if (!enabled || length <= 1 || intervalMs <= 0) return
    const id = setInterval(() => setIndex((i) => nextRotationIndex(i, length)), intervalMs)
    return () => clearInterval(id)
  }, [length, intervalMs, enabled])
  // Clamp derived at read time so a shrinking list never points out of range.
  return length > 0 ? index % length : 0
}
