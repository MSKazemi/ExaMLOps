import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { detectLocale, localeDir, translate, translatePlural, type Locale } from '@/lib/i18n'
import { I18nContext } from './i18nContext'

// i18n provider (F19 / ADR 0071). Holds the active locale, persists it, and keeps `<html lang/dir>` in
// sync for RTL-safety + a11y (R5). The `useI18n`/`useT` hooks live in `./i18nContext`.

const STORAGE_KEY = 'dashboard.locale'

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => {
    if (typeof localStorage !== 'undefined') {
      const saved = localStorage.getItem(STORAGE_KEY)
      if (saved === 'en' || saved === 'it') return saved
    }
    return detectLocale()
  })

  const setLocale = useCallback((l: Locale) => {
    setLocaleState(l)
    if (typeof localStorage !== 'undefined') localStorage.setItem(STORAGE_KEY, l)
  }, [])

  // Keep the document's language + direction current (side-effect only — no state update, R5).
  useEffect(() => {
    if (typeof document !== 'undefined') {
      document.documentElement.lang = locale
      document.documentElement.dir = localeDir(locale)
    }
  }, [locale])

  const value = useMemo(
    () => ({
      locale,
      setLocale,
      t: (key: string, vars?: Record<string, string | number>) => translate(locale, key, vars),
      tp: (key: string, count: number, vars?: Record<string, string | number>) =>
        translatePlural(locale, key, count, vars),
    }),
    [locale, setLocale],
  )

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}
