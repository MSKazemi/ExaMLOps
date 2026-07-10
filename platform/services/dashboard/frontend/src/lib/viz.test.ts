import { describe, it, expect } from 'vitest'
import {
  thresholdTone,
  toneStatus,
  histogram,
  formatDelta,
  ciLabel,
  sparklinePoints,
} from './viz'

describe('thresholdTone (F4 R2)', () => {
  it('higher-worse: crit ≥ crit, warn ≥ warn, else ok', () => {
    const t = { warn: 5, crit: 10 }
    expect(thresholdTone(12, t)).toBe('crit')
    expect(thresholdTone(7, t)).toBe('warn')
    expect(thresholdTone(2, t)).toBe('ok')
  })
  it('lower-worse inverts the comparison', () => {
    const t = { warn: 0.9, crit: 0.8 }
    expect(thresholdTone(0.75, t, 'lower-worse')).toBe('crit')
    expect(thresholdTone(0.85, t, 'lower-worse')).toBe('warn')
    expect(thresholdTone(0.95, t, 'lower-worse')).toBe('ok')
  })
  it('no threshold ⇒ ok', () => {
    expect(thresholdTone(999)).toBe('ok')
  })
})

describe('toneStatus', () => {
  it('maps to F3 status tokens', () => {
    expect(toneStatus('ok')).toBe('healthy')
    expect(toneStatus('warn')).toBe('warn')
    expect(toneStatus('crit')).toBe('critical')
  })
})

describe('histogram (F4 R5)', () => {
  it('bins values into equal-width buckets, max in last bucket', () => {
    const bins = histogram([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 5)
    expect(bins).toHaveLength(5)
    expect(bins.reduce((s, b) => s + b.count, 0)).toBe(11)
    expect(bins[4].count).toBeGreaterThan(0) // 10 landed in the last bucket
  })
  it('handles a degenerate (all-equal) series', () => {
    expect(histogram([3, 3, 3], 4)).toEqual([{ x0: 3, x1: 3, count: 3 }])
  })
  it('empty input ⇒ empty', () => {
    expect(histogram([], 5)).toEqual([])
  })
})

describe('formatDelta / ciLabel', () => {
  it('signs deltas with a real minus', () => {
    expect(formatDelta(3.2, '%')).toBe('+3.2%')
    expect(formatDelta(-1)).toBe('−1')
    expect(formatDelta(0)).toBe('0')
  })
  it('formats a CI', () => {
    expect(ciLabel([1.234, 5.678])).toBe('[1.23, 5.68]')
  })
})

describe('sparklinePoints (F4 R2)', () => {
  it('maps a series into fitted SVG points', () => {
    const pts = sparklinePoints([0, 5, 10], 100, 10).split(' ')
    expect(pts).toHaveLength(3)
    expect(pts[0]).toBe('0.0,10.0') // min → bottom
    expect(pts[2]).toBe('100.0,0.0') // max → top
  })
  it('renders a mid-line for a single point', () => {
    expect(sparklinePoints([7], 80, 20)).toBe('0,10 80,10')
  })
})
