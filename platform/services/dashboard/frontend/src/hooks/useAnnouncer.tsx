import { useCallback, useRef, useState, type ReactNode } from 'react'
import { AnnouncerContext, type AnnouncerPriority } from './announcer'

// Polite/assertive screen-reader announcer provider (F18 / ADR 0068, R3). Live/pushed content (F8)
// announces through this without stealing focus. A single visually-hidden aria-live region is mounted
// once at the app root. The `useAnnouncer` hook lives in `./announcer` (component-only export here).

export function AnnouncerProvider({ children }: { children: ReactNode }) {
  const [polite, setPolite] = useState('')
  const [assertive, setAssertive] = useState('')
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const announce = useCallback((message: string, priority: AnnouncerPriority = 'polite') => {
    const set = priority === 'assertive' ? setAssertive : setPolite
    // Clear first so a repeat of the same string still triggers an announcement, then set on next tick.
    set('')
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => set(message), 50)
  }, [])

  return (
    <AnnouncerContext.Provider value={{ announce }}>
      {children}
      <div className="sr-only" aria-live="polite" aria-atomic="true" role="status" data-testid="announcer-polite">
        {polite}
      </div>
      <div className="sr-only" aria-live="assertive" aria-atomic="true" role="alert" data-testid="announcer-assertive">
        {assertive}
      </div>
    </AnnouncerContext.Provider>
  )
}
