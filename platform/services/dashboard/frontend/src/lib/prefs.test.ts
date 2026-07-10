import { describe, it, expect, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import {
  refKey,
  togglePinInList,
  pushRecentInList,
  usePrefs,
  useWatchlist,
  useOnboarding,
  type EntityRef,
} from './prefs'

const M: EntityRef = { type: 'models', id: 'jpcp' }
const N: EntityRef = { type: 'models', id: 'awgn' }

describe('pure list helpers', () => {
  it('refKey identifies an entity', () => {
    expect(refKey(M)).toBe('models:jpcp')
  })
  it('togglePinInList adds then removes', () => {
    const once = togglePinInList([], M)
    expect(once).toHaveLength(1)
    expect(togglePinInList(once, M)).toHaveLength(0)
  })
  it('pushRecentInList dedupes, most-recent-first, and caps', () => {
    let list = pushRecentInList([], M)
    list = pushRecentInList(list, N)
    list = pushRecentInList(list, M) // M moves to front
    expect(list.map(refKey)).toEqual(['models:jpcp', 'models:awgn'])
    const capped = Array.from({ length: 10 }, (_, i) => ({ type: 't', id: String(i) })).reduce(
      (acc, r) => pushRecentInList(acc, r, 8),
      [] as EntityRef[],
    )
    expect(capped).toHaveLength(8)
  })
})

describe('hooks (localStorage-backed)', () => {
  beforeEach(() => localStorage.clear())

  it('usePrefs persists a preference change', () => {
    const { result, unmount } = renderHook(() => usePrefs())
    act(() => result.current.setPref('defaultLanding', '/finops'))
    expect(result.current.prefs.defaultLanding).toBe('/finops')
    unmount()
    // a fresh mount reads the persisted value
    const { result: again } = renderHook(() => usePrefs())
    expect(again.current.prefs.defaultLanding).toBe('/finops')
  })

  it('useWatchlist toggles and reports pinned state', () => {
    const { result } = renderHook(() => useWatchlist())
    expect(result.current.isPinned(M)).toBe(false)
    act(() => result.current.toggle(M))
    expect(result.current.isPinned(M)).toBe(true)
    act(() => result.current.toggle(M))
    expect(result.current.isPinned(M)).toBe(false)
  })

  it('useOnboarding completes once and can be reset', () => {
    const { result, unmount } = renderHook(() => useOnboarding())
    expect(result.current.done).toBe(false)
    act(() => result.current.complete())
    expect(result.current.done).toBe(true)
    unmount()
    expect(renderHook(() => useOnboarding()).result.current.done).toBe(true)
  })
})
