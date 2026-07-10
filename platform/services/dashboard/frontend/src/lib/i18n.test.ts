import { describe, it, expect, vi } from 'vitest'
import {
  translate,
  translatePlural,
  formatNumber,
  formatPercent,
  formatHpcUnit,
  formatRelativeTime,
  formatInTz,
  utcTooltip,
  detectLocale,
  localeDir,
  LOCALES,
} from './i18n'

describe('translate', () => {
  it('resolves namespaced keys per locale', () => {
    expect(translate('en', 'finops.title')).toBe('FinOps & Green-AI')
    expect(translate('it', 'finops.title')).toBe('FinOps e Green-AI')
  })
  it('interpolates {{vars}}', () => {
    expect(translate('en', 'common.partial_data', { sources: 'cost' })).toBe('Partial data — cost unavailable')
  })
  it('falls back to EN then to the raw key (and warns) on a miss', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    expect(translate('it', 'common.nope')).toBe('common.nope')
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })
})

describe('translatePlural', () => {
  it('selects one vs other by count', () => {
    expect(translatePlural('en', 'common.models', 1)).toBe('1 model')
    expect(translatePlural('en', 'common.models', 3)).toBe('3 models')
    expect(translatePlural('it', 'common.models', 1)).toBe('1 modello')
    expect(translatePlural('it', 'common.models', 5)).toBe('5 modelli')
  })
})

describe('Intl formatters', () => {
  it('formats numbers with locale conventions', () => {
    // Italian uses ',' as the decimal separator
    expect(formatNumber(1234.5, 'it', { maximumFractionDigits: 1 })).toContain(',')
    expect(formatNumber(1234.5, 'en', { maximumFractionDigits: 1 })).toContain('.')
  })
  it('formats percentages', () => {
    expect(formatPercent(0.25, 'en')).toBe('25%')
  })
  it('formats relative time', () => {
    const now = 1_000_000_000_000
    expect(formatRelativeTime(now - 3600_000, now, 'en')).toMatch(/hour/)
  })
})

describe('formatHpcUnit (R3)', () => {
  it('SI-scales throughput and carbon', () => {
    expect(formatHpcUnit(2.5e9, 'FLOPS')).toBe('2.5 GFLOPS')
    expect(formatHpcUnit(1500, 'gCO2e')).toBe('1.5 kgCO2e')
  })
  it('leaves GPU-hours unscaled', () => {
    expect(formatHpcUnit(12.5, 'GPU-h')).toBe('12.5 GPU-h')
  })
  it('keeps small values in the base unit', () => {
    expect(formatHpcUnit(42, 'W')).toBe('42 W')
  })
})

describe('timezone helpers (R4)', () => {
  it('formats in an explicit timezone and emits a UTC tooltip', () => {
    const iso = '2026-07-10T12:00:00Z'
    expect(formatInTz(iso, 'UTC', 'en')).toMatch(/2026/)
    expect(utcTooltip(iso)).toBe('2026-07-10 12:00:00 UTC')
  })
})

describe('locale detection + direction', () => {
  it('constrains to shipped locales', () => {
    expect(LOCALES.map((l) => l.code)).toEqual(['en', 'it'])
    expect(['en', 'it']).toContain(detectLocale())
  })
  it('reports layout direction', () => {
    expect(localeDir('en')).toBe('ltr')
  })
})
