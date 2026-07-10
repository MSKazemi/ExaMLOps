// Accessibility helpers — pure, dependency-free (F18 / ADR 0068).
//
// WCAG 2.2 AA math (contrast) plus small environment probes. The design tokens (F3) are authored in the
// OKLCH colour space, so the contrast audit converts OKLCH → linear sRGB → relative luminance to verify
// AA on the *actual* token values (R4), not a hex approximation.

// ── OKLCH → relative luminance ────────────────────────────────────────────────

export interface Oklch {
  l: number // 0..1
  c: number // chroma
  h: number // hue degrees
}

/** Parse a CSS `oklch(L C H)` string (alpha, if any, is ignored). Returns null if unparseable. */
export function parseOklch(input: string): Oklch | null {
  const m = input.match(/oklch\(\s*([\d.]+%?)\s+([\d.]+)\s+([\d.]+)/i)
  if (!m) return null
  const l = m[1].endsWith('%') ? parseFloat(m[1]) / 100 : parseFloat(m[1])
  return { l, c: parseFloat(m[2]), h: parseFloat(m[3]) }
}

/** WCAG relative luminance of an OKLCH colour (channels clamped to the sRGB gamut). */
export function oklchLuminance({ l: L, c: C, h: H }: Oklch): number {
  const hr = (H * Math.PI) / 180
  const a = C * Math.cos(hr)
  const b = C * Math.sin(hr)
  // OKLab → LMS' (Björn Ottosson's matrix)
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b
  const s_ = L - 0.0894841775 * a - 1.291485548 * b
  const l3 = l_ ** 3
  const m3 = m_ ** 3
  const s3 = s_ ** 3
  // LMS → linear sRGB
  const r = 4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3
  const g = -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3
  const bl = -0.0041960863 * l3 - 0.7034186147 * m3 + 1.707614701 * s3
  const clamp = (x: number) => Math.min(1, Math.max(0, x))
  // linear-sRGB channels → WCAG relative luminance
  return 0.2126 * clamp(r) + 0.7152 * clamp(g) + 0.0722 * clamp(bl)
}

/** WCAG contrast ratio between two relative luminances (order-independent), 1..21. */
export function contrastRatio(lum1: number, lum2: number): number {
  const hi = Math.max(lum1, lum2)
  const lo = Math.min(lum1, lum2)
  return (hi + 0.05) / (lo + 0.05)
}

/** Contrast ratio between two OKLCH colours (accepts strings or parsed values). */
export function oklchContrast(fg: string | Oklch, bg: string | Oklch): number {
  const f = typeof fg === 'string' ? parseOklch(fg) : fg
  const b = typeof bg === 'string' ? parseOklch(bg) : bg
  if (!f || !b) return 1
  return contrastRatio(oklchLuminance(f), oklchLuminance(b))
}

/** WCAG 2.2 AA pass: 4.5:1 for normal text, 3:1 for large text (≥18.66px bold / ≥24px). */
export function meetsAA(ratio: number, opts: { large?: boolean } = {}): boolean {
  return ratio >= (opts.large ? 3 : 4.5)
}

/** WCAG AAA pass: 7:1 normal, 4.5:1 large. */
export function meetsAAA(ratio: number, opts: { large?: boolean } = {}): boolean {
  return ratio >= (opts.large ? 4.5 : 7)
}

// ── environment probes (guarded for SSR / jsdom) ──────────────────────────────

/** True when the user has requested reduced motion (R5). Safe when matchMedia is absent. */
export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/** Ordered list of tabbable elements within a container (for focus trapping, R2). */
export function tabbableWithin(container: HTMLElement): HTMLElement[] {
  const sel =
    'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
  // Exclude explicitly hidden elements. Deliberately not using offsetParent (unreliable under jsdom and
  // for position:fixed), so the trap stays testable and correct for overlay dialogs.
  return Array.from(container.querySelectorAll<HTMLElement>(sel)).filter(
    (el) => !el.hasAttribute('hidden') && el.getAttribute('aria-hidden') !== 'true',
  )
}
