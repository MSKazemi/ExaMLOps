import { describe, it, expect, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import {
  matchBreakpoint,
  isKioskMode,
  nextRotationIndex,
  popOutFeatures,
  popOut,
  useRotator,
} from './responsive'

describe('matchBreakpoint', () => {
  it('classifies widths into breakpoints', () => {
    expect(matchBreakpoint(1024)).toBe('laptop')
    expect(matchBreakpoint(1400)).toBe('desktop')
    expect(matchBreakpoint(1920)).toBe('wide')
    expect(matchBreakpoint(3000)).toBe('ultrawide')
  })
})

describe('isKioskMode', () => {
  it('detects ?kiosk=1 / true', () => {
    expect(isKioskMode('?kiosk=1')).toBe(true)
    expect(isKioskMode('?kiosk=true')).toBe(true)
    expect(isKioskMode('?foo=bar')).toBe(false)
    expect(isKioskMode('')).toBe(false)
  })
})

describe('nextRotationIndex', () => {
  it('wraps around', () => {
    expect(nextRotationIndex(0, 3)).toBe(1)
    expect(nextRotationIndex(2, 3)).toBe(0)
    expect(nextRotationIndex(0, 0)).toBe(0)
  })
})

describe('popOut', () => {
  it('builds a popup feature string', () => {
    expect(popOutFeatures(800, 600)).toContain('width=800,height=600')
    expect(popOutFeatures()).toContain('noopener')
  })
  it('opens a window and reports success/blocked', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue({} as Window)
    expect(popOut('http://x/panel')).toBe(true)
    expect(open).toHaveBeenCalledWith('http://x/panel', 'examlops-panel', expect.stringContaining('popup'))
    open.mockReturnValue(null)
    expect(popOut('http://x/panel')).toBe(false)
    open.mockRestore()
  })
})

describe('useRotator', () => {
  it('advances on the interval and wraps (R2)', () => {
    vi.useFakeTimers()
    try {
      const { result } = renderHook(() => useRotator(3, 1000))
      expect(result.current).toBe(0)
      act(() => void vi.advanceTimersByTime(1000))
      expect(result.current).toBe(1)
      act(() => void vi.advanceTimersByTime(2000))
      expect(result.current).toBe(0) // 1 → 2 → 0 (wrap)
    } finally {
      vi.useRealTimers()
    }
  })
  it('does not rotate a single-item list', () => {
    vi.useFakeTimers()
    try {
      const { result } = renderHook(() => useRotator(1, 1000))
      act(() => void vi.advanceTimersByTime(5000))
      expect(result.current).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })
})
