# Internationalization & Localization

The dashboard has a lightweight, **dependency-free** i18n layer: externalized copy in namespaced catalogs
(English + Italian), locale-aware `Intl` formatting, and a **shared HPC-unit formatter** used across the
facility and FinOps surfaces.

- **Feature:** F19 · **Design:** [ADR 0071](../../design/adr/0071-dashboard-i18n-localization.md) ·
  **Spec:** `design/vision/specs/F19-i18n-localization.md`
- **Frontend:** `lib/i18n.ts` (pure), `hooks/i18nContext.ts` + `hooks/I18nProvider.tsx`,
  `components/LocaleSwitcher.tsx`

> The spec named react-i18next; we ship the same capability without the dependency to keep the build lean
> (adding npm deps invalidates the CI node-modules cache). The catalog structure supports adding locales.

## Translating copy

```tsx
import { useI18n } from '@/hooks/i18nContext'

function Title() {
  const { t, tp } = useI18n()
  return (
    <>
      <h1>{t('finops.title')}</h1>
      <p>{tp('common.models', count)}</p>            {/* "1 model" / "3 models" — Intl.PluralRules */}
      <span>{t('common.partial_data', { sources: 'cost' })}</span>   {/* {{var}} interpolation */}
    </>
  )
}
```

Keys are `namespace.name` (e.g. `finops.title`). A missing key falls back to English, then to the raw key
with a `console.warn` — visible but never a crash.

## Formatting numbers, dates, and HPC units

All formatting is locale-aware via `Intl`:

```ts
import { formatNumber, formatPercent, formatRelativeTime, formatHpcUnit, formatInTz, utcTooltip } from '@/lib/i18n'

formatNumber(1234.5, 'it', { maximumFractionDigits: 1 })  // "1.234,5"
formatPercent(0.25, 'en')                                  // "25%"
formatHpcUnit(2.5e9, 'FLOPS')                              // "2.5 GFLOPS"  (shared HPC formatter)
formatHpcUnit(1500, 'gCO2e')                               // "1.5 kgCO2e"
formatHpcUnit(12.5, 'GPU-h')                               // "12.5 GPU-h"  (unscaled)
formatInTz(iso, userTz)                                    // time in the user's TZ …
utcTooltip(iso)                                            // … with a UTC reference for the tooltip
```

**Always use `formatHpcUnit` for throughput / power / energy / carbon / GPU-hours** so F6 and F13 render
units identically (R3).

## Locale selection

The `LocaleSwitcher` (in the shell) changes the active locale; all copy re-renders immediately. The
choice is detected from the browser on first load and persisted to `localStorage`. The provider keeps
`<html lang>` and `dir` current, so layout stays RTL-safe through F3's logical CSS properties.

## Notes & limits

Shipped: EN + IT catalogs, `t`/`tp`, Intl + HPC formatters, TZ helpers, the switcher, and first adoption
(FinOps). Deferred (tracked in the plan): a **CI guard** against hardcoded strings (R1 — needs an eslint
plugin), a **pseudo-localization** overflow test (R5), full catalog coverage, and the F6 **facility-local
TZ** toggle. Locale preference persistence graduates to F21 workspaces.

See [`docs/dashboard/architecture.md`](../dashboard/architecture.md#internationalization--localization-f19)
for the design diagram.
