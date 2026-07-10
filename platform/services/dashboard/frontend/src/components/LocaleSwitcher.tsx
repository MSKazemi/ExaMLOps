import { Languages } from 'lucide-react'
import { LOCALES, type Locale } from '@/lib/i18n'
import { useI18n } from '@/hooks/i18nContext'

// Locale picker (F19 / ADR 0071). Switching re-renders all copy through the i18n provider (GWT-1).
export function LocaleSwitcher() {
  const { locale, setLocale, t } = useI18n()
  return (
    <label className="flex items-center gap-1.5 text-xs text-muted-foreground" title={t('common.language')}>
      <Languages className="size-3.5" aria-hidden="true" />
      <span className="sr-only">{t('common.language')}</span>
      <select
        value={locale}
        onChange={(e) => setLocale(e.target.value as Locale)}
        aria-label={t('common.language')}
        className="bg-transparent text-xs outline-none"
      >
        {LOCALES.map((l) => (
          <option key={l.code} value={l.code}>
            {l.label}
          </option>
        ))}
      </select>
    </label>
  )
}
