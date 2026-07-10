import { createContext, useContext } from 'react'
import { translate, translatePlural, type Locale } from '@/lib/i18n'

// i18n context + hooks (F19 / ADR 0071). Hook-only module (no component export) so the provider file can
// export a component only — avoids a react-refresh warning.

export interface I18nApi {
  locale: Locale
  setLocale: (l: Locale) => void
  t: (key: string, vars?: Record<string, string | number>) => string
  tp: (key: string, count: number, vars?: Record<string, string | number>) => string
}

export const I18nContext = createContext<I18nApi>({
  locale: 'en',
  setLocale: () => {},
  t: (key, vars) => translate('en', key, vars),
  tp: (key, count, vars) => translatePlural('en', key, count, vars),
})

/** Full i18n API (locale + setLocale + t/tp). */
export function useI18n(): I18nApi {
  return useContext(I18nContext)
}

/** Convenience: just the translate function. */
export function useT(): I18nApi['t'] {
  return useContext(I18nContext).t
}
