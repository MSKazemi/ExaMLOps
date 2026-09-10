// Lightweight, dependency-free i18n core (F19 / ADR 0071).
//
// Externalized namespaced catalogs (EN + one EU locale, Italian) + `translate()` with interpolation and
// Intl-based pluralization, plus locale-aware Intl formatters and a **shared HPC-unit formatter** used
// across F6/F13 (R3). Kept pure so every formatter/catalog rule is unit-tested; the React binding
// (provider/hook) lives in `hooks/`.

export type Locale = 'en' | 'it'

export const LOCALES: { code: Locale; label: string; dir: 'ltr' | 'rtl' }[] = [
  { code: 'en', label: 'English', dir: 'ltr' },
  { code: 'it', label: 'Italiano', dir: 'ltr' },
]

// Namespaced message catalogs. Values may contain `{{var}}` placeholders and `_one`/`_other` plural
// variants (selected via Intl.PluralRules). Adding a locale = adding one entry here (R2).
type Catalog = Record<string, Record<string, string>>

export const CATALOGS: Record<Locale, Catalog> = {
  en: {
    common: {
      loading: 'Loading…',
      search: 'Search',
      language: 'Language',
      partial_data: 'Partial data — {{sources}} unavailable',
      models_one: '{{count}} model',
      models_other: '{{count}} models',
    },
    finops: {
      title: 'FinOps & Green-AI',
      total_spend: 'Total spend',
      gpu_hours: 'GPU-hours',
      carbon: 'Operational carbon (est.)',
      cost_by_model: 'Cost by model',
    },
  },
  it: {
    common: {
      loading: 'Caricamento…',
      search: 'Cerca',
      language: 'Lingua',
      partial_data: 'Dati parziali — {{sources}} non disponibili',
      models_one: '{{count}} modello',
      models_other: '{{count}} modelli',
    },
    finops: {
      title: 'FinOps e Green-AI',
      total_spend: 'Spesa totale',
      gpu_hours: 'Ore-GPU',
      carbon: 'Carbonio operativo (stima)',
      cost_by_model: 'Costo per modello',
    },
  },
}

function lookup(locale: Locale, key: string): string | undefined {
  const [ns, name] = key.includes('.') ? key.split('.', 2) : ['common', key]
  return CATALOGS[locale]?.[ns]?.[name] ?? CATALOGS.en?.[ns]?.[name]
}

function interpolate(template: string, vars?: Record<string, string | number>): string {
  if (!vars) return template
  return template.replace(/\{\{(\w+)\}\}/g, (_, k) => (k in vars ? String(vars[k]) : `{{${k}}}`))
}

/**
 * Translate `ns.key` for `locale` with `{{var}}` interpolation. Falls back to EN, then to the raw key
 * (and warns once) so a missing translation is visible but never crashes (R1/observability).
 */
export function translate(locale: Locale, key: string, vars?: Record<string, string | number>): string {
  const template = lookup(locale, key)
  if (template === undefined) {
    if (typeof console !== 'undefined') console.warn(`[i18n] missing key: ${key}`)
    return key
  }
  return interpolate(template, vars)
}

/** Plural-aware translate: picks `<key>_one`/`<key>_other` via Intl.PluralRules and injects `count`. */
export function translatePlural(
  locale: Locale,
  key: string,
  count: number,
  vars?: Record<string, string | number>,
): string {
  const category = new Intl.PluralRules(locale).select(count)
  const variant = lookup(locale, `${key}_${category}`) !== undefined ? `${key}_${category}` : `${key}_other`
  return translate(locale, variant, { count, ...vars })
}

// ── Intl formatters (R3) ──────────────────────────────────────────────────────

export function formatNumber(value: number, locale: Locale, opts?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(locale, opts).format(value)
}

export function formatPercent(ratio: number, locale: Locale, digits = 0): string {
  return new Intl.NumberFormat(locale, {
    style: 'percent',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(ratio)
}

export function formatDate(iso: string | number | Date, locale: Locale, opts?: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat(locale, opts ?? { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(iso))
}

/** Relative time (e.g. "3 hours ago") in the given locale. */
export function formatRelativeTime(fromIso: string | number | Date, nowMs: number, locale: Locale): string {
  const deltaSec = Math.round((new Date(fromIso).getTime() - nowMs) / 1000)
  const abs = Math.abs(deltaSec)
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' })
  const steps: [number, Intl.RelativeTimeFormatUnit][] = [
    [60, 'second'],
    [3600, 'minute'],
    [86400, 'hour'],
    [604800, 'day'],
  ]
  if (abs < 60) return rtf.format(deltaSec, 'second')
  for (let i = 1; i < steps.length; i++) {
    if (abs < steps[i][0]) return rtf.format(Math.round(deltaSec / steps[i - 1][0]), steps[i][1])
  }
  return rtf.format(Math.round(deltaSec / 604800), 'week')
}

// ── shared HPC-unit formatter (R3) ─────────────────────────────────────────────

export type HpcUnit = 'FLOPS' | 'B/s' | 'W' | 'Wh' | 'gCO2e' | 'GPU-h'

const _SI = [
  { factor: 1e12, prefix: 'T' },
  { factor: 1e9, prefix: 'G' },
  { factor: 1e6, prefix: 'M' },
  { factor: 1e3, prefix: 'k' },
]

/**
 * Format an HPC quantity with SI scaling, consistently across F6/F13 (R3). E.g. 2.5e9 FLOPS → "2.5 GFLOPS",
 * 1500 gCO2e → "1.5 kgCO2e". GPU-h are not SI-scaled (they read naturally as-is).
 */
export function formatHpcUnit(value: number, unit: HpcUnit, locale: Locale = 'en'): string {
  if (unit === 'GPU-h') return `${formatNumber(value, locale, { maximumFractionDigits: 1 })} ${unit}`
  const abs = Math.abs(value)
  for (const { factor, prefix } of _SI) {
    if (abs >= factor) {
      return `${formatNumber(value / factor, locale, { maximumFractionDigits: 2 })} ${prefix}${unit}`
    }
  }
  return `${formatNumber(value, locale, { maximumFractionDigits: 2 })} ${unit}`
}

// ── timezone (R4) ──────────────────────────────────────────────────────────────

/** Format a timestamp in a specific timezone (user TZ or facility-local). */
export function formatInTz(iso: string | number | Date, timeZone: string, locale: Locale = 'en'): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short', timeZone }).format(new Date(iso))
}

/** UTC label for a tooltip alongside the localized time (R4). */
export function utcTooltip(iso: string | number | Date): string {
  return `${new Date(iso).toISOString().replace('T', ' ').slice(0, 19)} UTC`
}

// ── locale detection / direction ────────────────────────────────────────────────

/** Best-effort locale from the browser, constrained to shipped locales (defaults to EN). */
export function detectLocale(): Locale {
  if (typeof navigator === 'undefined') return 'en'
  const lang = (navigator.language || 'en').slice(0, 2).toLowerCase()
  return LOCALES.some((l) => l.code === lang) ? (lang as Locale) : 'en'
}

/** Layout direction for a locale (RTL-safe hook point, R5). */
export function localeDir(locale: Locale): 'ltr' | 'rtl' {
  return LOCALES.find((l) => l.code === locale)?.dir ?? 'ltr'
}
