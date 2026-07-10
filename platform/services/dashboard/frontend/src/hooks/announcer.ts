import { createContext, useContext } from 'react'

// Announcer context + hook (F18 / ADR 0068, R3). Kept in a hook-only module (no component export) so the
// provider file can export a component only — avoids a react-refresh fast-refresh warning.

export type AnnouncerPriority = 'polite' | 'assertive'

export interface AnnouncerApi {
  announce: (message: string, priority?: AnnouncerPriority) => void
}

export const AnnouncerContext = createContext<AnnouncerApi>({ announce: () => {} })

/** Access the polite/assertive announcer. Returns a no-op when no provider is mounted. */
export function useAnnouncer(): AnnouncerApi {
  return useContext(AnnouncerContext)
}
