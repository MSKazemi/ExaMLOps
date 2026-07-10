// F4 visualization system — pure, dependency-free helpers (ADR 0055).
//
// The chart *components* live in `components/viz/`; the math/formatting lives here so it is
// unit-testable in isolation and shared across charts. Colour comes from F3 status tokens
// (never hardcoded), so every chart is themeable + colour-blind-safe (a label always accompanies).

export type VizTone = 'ok' | 'warn' | 'crit'

export interface Threshold {
  warn: number
  crit: number
}

/**
 * Map a value onto an ok/warn/crit tone against thresholds (F4 R2 threshold colouring).
 *
 * `direction: 'higher-worse'` (default) suits drift/latency/error metrics where a larger value is
 * worse; `'lower-worse'` suits accuracy/throughput where a smaller value is worse.
 */
export function thresholdTone(
  value: number,
  threshold?: Threshold,
  direction: 'higher-worse' | 'lower-worse' = 'higher-worse',
): VizTone {
  if (!threshold) return 'ok'
  const { warn, crit } = threshold
  if (direction === 'higher-worse') {
    if (value >= crit) return 'crit'
    if (value >= warn) return 'warn'
    return 'ok'
  }
  if (value <= crit) return 'crit'
  if (value <= warn) return 'warn'
  return 'ok'
}

/** Map a viz tone to the F3 status token consumed by `StatusPill` / `statusMeta`. */
export function toneStatus(tone: VizTone): 'healthy' | 'warn' | 'critical' {
  return tone === 'ok' ? 'healthy' : tone === 'warn' ? 'warn' : 'critical'
}

export interface Bin {
  x0: number
  x1: number
  count: number
}

/**
 * Bin values into a histogram (F4 R5 distribution). Returns `bins` equal-width buckets spanning
 * [min, max]; the max value falls in the last bucket. Empty input ⇒ empty array.
 */
export function histogram(values: number[], bins = 10): Bin[] {
  if (values.length === 0 || bins < 1) return []
  const min = Math.min(...values)
  const max = Math.max(...values)
  if (min === max) {
    return [{ x0: min, x1: max, count: values.length }]
  }
  const width = (max - min) / bins
  const out: Bin[] = Array.from({ length: bins }, (_, i) => ({
    x0: min + i * width,
    x1: min + (i + 1) * width,
    count: 0,
  }))
  for (const v of values) {
    let idx = Math.floor((v - min) / width)
    if (idx >= bins) idx = bins - 1 // max lands in the last bucket
    if (idx < 0) idx = 0
    out[idx].count++
  }
  return out
}

/** Signed delta label, e.g. `+3.2%` / `−1.0` (uses a real minus sign). */
export function formatDelta(delta: number, unit = ''): string {
  const sign = delta > 0 ? '+' : delta < 0 ? '−' : ''
  return `${sign}${Math.abs(delta)}${unit}`
}

/** Format a confidence interval `[lo, hi]` for display (F4 R5 uncertainty). */
export function ciLabel([lo, hi]: [number, number], digits = 2): string {
  return `[${lo.toFixed(digits)}, ${hi.toFixed(digits)}]`
}

/**
 * Build the `points` attribute for an SVG polyline sparkline over `values`, fitted to a
 * `width`×`height` box (F4 R2 trend). Flat/empty series render a mid-line.
 */
export function sparklinePoints(values: number[], width = 80, height = 20): string {
  if (values.length === 0) return ''
  if (values.length === 1) return `0,${height / 2} ${width},${height / 2}`
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const step = width / (values.length - 1)
  return values
    .map((v, i) => {
      const x = i * step
      const y = height - ((v - min) / span) * height
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}
