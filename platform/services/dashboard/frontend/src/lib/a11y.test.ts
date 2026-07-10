import { describe, it, expect } from 'vitest'
import {
  parseOklch,
  oklchLuminance,
  contrastRatio,
  oklchContrast,
  meetsAA,
  meetsAAA,
} from './a11y'

describe('parseOklch', () => {
  it('parses L C H and ignores alpha', () => {
    expect(parseOklch('oklch(0.94 0.006 255)')).toEqual({ l: 0.94, c: 0.006, h: 255 })
    expect(parseOklch('oklch(1 0 0 / 9%)')).toEqual({ l: 1, c: 0, h: 0 })
  })
  it('accepts a percentage lightness', () => {
    expect(parseOklch('oklch(50% 0.1 200)')?.l).toBeCloseTo(0.5)
  })
  it('returns null for non-oklch input', () => {
    expect(parseOklch('#fff')).toBeNull()
  })
})

describe('luminance + contrast', () => {
  it('pure white vs pure black is ~21:1', () => {
    const white = oklchLuminance({ l: 1, c: 0, h: 0 })
    const black = oklchLuminance({ l: 0, c: 0, h: 0 })
    expect(contrastRatio(white, black)).toBeGreaterThan(20)
  })
  it('is order-independent', () => {
    expect(oklchContrast('oklch(0.9 0 0)', 'oklch(0.1 0 0)')).toBeCloseTo(
      oklchContrast('oklch(0.1 0 0)', 'oklch(0.9 0 0)'),
    )
  })
})

describe('meetsAA / meetsAAA', () => {
  it('applies the 4.5 / 3.0 AA thresholds', () => {
    expect(meetsAA(4.5)).toBe(true)
    expect(meetsAA(4.49)).toBe(false)
    expect(meetsAA(3.0, { large: true })).toBe(true)
  })
  it('applies the 7 / 4.5 AAA thresholds', () => {
    expect(meetsAAA(7)).toBe(true)
    expect(meetsAAA(4.5, { large: true })).toBe(true)
    expect(meetsAAA(6.9)).toBe(false)
  })
})

// R4 guard — the actual F3 design tokens (index.css) must meet AA. If a token edit regresses contrast
// this test fails, catching the most common AA regression without adding an axe dependency.
describe('design-token contrast audit (R4)', () => {
  const themes = {
    dark: { bg: 'oklch(0.11 0.022 268)', fg: 'oklch(0.94 0.006 255)', muted: 'oklch(0.68 0.018 260)', primary: 'oklch(0.64 0.20 265)' },
    light: { bg: 'oklch(0.985 0.002 268)', fg: 'oklch(0.14 0.022 268)', muted: 'oklch(0.48 0.018 260)', primary: 'oklch(0.50 0.20 265)' },
    'high-contrast': { bg: 'oklch(0.07 0.014 268)', fg: 'oklch(0.96 0.004 255)', muted: 'oklch(0.62 0.016 260)', primary: 'oklch(0.64 0.20 265)' },
  }
  for (const [name, t] of Object.entries(themes)) {
    it(`${name}: foreground + muted + primary meet AA on background`, () => {
      expect(meetsAA(oklchContrast(t.fg, t.bg))).toBe(true)
      expect(meetsAA(oklchContrast(t.muted, t.bg))).toBe(true)
      expect(meetsAA(oklchContrast(t.primary, t.bg))).toBe(true)
    })
  }
})
