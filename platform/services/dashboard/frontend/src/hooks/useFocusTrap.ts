import { useEffect, type RefObject } from 'react'
import { tabbableWithin } from '@/lib/a11y'

/**
 * Trap keyboard focus within `ref` while `active`, and restore focus to the previously-focused element
 * when it deactivates or unmounts (F18 / ADR 0068, R2). Tab/Shift-Tab wrap around the container's
 * tabbable elements; focus moves to the first tabbable on activation.
 */
export function useFocusTrap(ref: RefObject<HTMLElement | null>, active: boolean): void {
  useEffect(() => {
    if (!active) return
    const container = ref.current
    if (!container) return

    const previouslyFocused = document.activeElement as HTMLElement | null

    // Move focus into the trap (first tabbable, else the container itself).
    const initial = tabbableWithin(container)
    if (initial.length > 0) initial[0].focus()
    else container.focus()

    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== 'Tab' || !container) return
      const tabbables = tabbableWithin(container)
      if (tabbables.length === 0) {
        e.preventDefault()
        return
      }
      const first = tabbables[0]
      const last = tabbables[tabbables.length - 1]
      const activeEl = document.activeElement
      if (e.shiftKey && activeEl === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && activeEl === last) {
        e.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      // Restore focus to where it was before the trap opened.
      previouslyFocused?.focus?.()
    }
  }, [ref, active])
}
