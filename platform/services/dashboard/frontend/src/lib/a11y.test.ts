/// <reference types="node" />
// Node typings are referenced for this file alone: the audit must read the *stylesheet*, and
// Vite's `?raw` import yields an empty string here because the Tailwind plugin claims `.css`.

import { readFileSync } from 'node:fs'

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
  // This used to audit four colours **transcribed into this file** under theme names the stylesheet
  // does not use (`dark`/`light`/`high-contrast`; the CSS ships `:root`, `day`, `midnight`). Editing
  // a colour in `index.css` could not fail it — it audited a copy, not the theme. It now reads the
  // stylesheet, and checks every foreground/background pair rather than four of roughly twenty-five.

  // Vitest runs with the package root as cwd.
  const css = readFileSync('src/index.css', 'utf8')

  const themes: Record<string, Record<string, string>> = {}
  for (const m of css.matchAll(/(:root|html\[data-theme="(\w+)"\])\s*\{/g)) {
    const name = m[2] ?? 'root'
    let depth = 0
    let end = m.index! + m[0].length - 1
    for (let j = end; j < css.length; j++) {
      if (css[j] === '{') depth++
      else if (css[j] === '}') {
        depth--
        if (depth === 0) { end = j; break }
      }
    }
    const body = css.slice(m.index! + m[0].length, end)
    const tokens: Record<string, string> = {}
    for (const t of body.matchAll(/--([\w-]+):\s*(oklch\([^)]*\))/g)) tokens[t[1]] = t[2]
    if (tokens.background && tokens.foreground && !(name in themes)) themes[name] = tokens
  }

  // Text/background pairs the UI actually renders. Borders and rings carry alpha and are not text,
  // so they are out of scope for a contrast ratio computed without compositing.
  const PAIRS: [string, string][] = [
    ['foreground', 'background'],
    ['muted-foreground', 'background'],
    ['primary', 'background'],
    ['card-foreground', 'card'],
    ['popover-foreground', 'popover'],
    ['secondary-foreground', 'secondary'],
    ['accent-foreground', 'accent'],
    ['destructive', 'background'],
    ['primary-foreground', 'primary'],
  ]

  // A recorded AA failure, with its number, because a guard that fails the build on a known issue
  // gets skipped and a silent one gets forgotten. `--primary` serves two roles at once — a surface
  // that carries text, and a foreground on the page background — and on a dark theme **no single
  // lightness satisfies both at AA**: white text on it needs L ≤ 0.565, while it needs L ≥ 0.58 to
  // stay readable on the background. Fixing it is a design decision, not a tweak: either give
  // `--primary-foreground` a dark value (5.66:1, but blue buttons get near-black labels) or split
  // the token into a surface colour and an accent. Measured 2026-09-14.
  const KNOWN_FAILURES: Record<string, string[]> = {
    root: ['primary-foreground on primary'],
    midnight: ['primary-foreground on primary'],
  }

  it('found the themes the stylesheet actually declares', () => {
    // Anti-vacuity: if the CSS structure changes and nothing parses, every check below passes on an
    // empty set — which looks exactly like compliance.
    expect(Object.keys(themes).sort()).toEqual(['day', 'midnight', 'root'])
    for (const t of Object.values(themes)) expect(Object.keys(t).length).toBeGreaterThan(10)
  })

  for (const [name, tokens] of Object.entries(themes)) {
    it(`${name}: every text pair meets AA, except those recorded`, () => {
      const known = KNOWN_FAILURES[name] ?? []
      const failures: string[] = []
      for (const [fg, bg] of PAIRS) {
        if (!tokens[fg] || !tokens[bg]) continue
        const label = `${fg} on ${bg}`
        const ratio = oklchContrast(tokens[fg], tokens[bg])
        if (!meetsAA(ratio) && !known.includes(label)) {
          failures.push(`${label} = ${ratio.toFixed(2)}:1`)
        }
      }
      expect(failures).toEqual([])
    })

    it(`${name}: every recorded failure still reproduces`, () => {
      // When one is fixed its entry must go, so the list cannot outlive the problem.
      for (const label of KNOWN_FAILURES[name] ?? []) {
        const [fg, bg] = label.split(' on ')
        expect(meetsAA(oklchContrast(tokens[fg], tokens[bg]))).toBe(false)
      }
    })
  }
})
