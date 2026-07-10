import { describe, it, expect } from 'vitest'
import { useRef, useState } from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import { useFocusTrap } from './useFocusTrap'

function Harness() {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useFocusTrap(ref, open)
  return (
    <div>
      <button onClick={() => setOpen(true)}>open</button>
      {open && (
        <div ref={ref} role="dialog">
          <button>first</button>
          <button onClick={() => setOpen(false)}>close</button>
        </div>
      )}
    </div>
  )
}

describe('useFocusTrap', () => {
  it('moves focus into the trap on activate and restores it on deactivate', () => {
    render(<Harness />)
    const opener = screen.getByRole('button', { name: 'open' })
    opener.focus()
    expect(document.activeElement).toBe(opener)

    fireEvent.click(opener)
    // focus moved to the first tabbable inside the dialog
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'first' }))

    fireEvent.click(screen.getByRole('button', { name: 'close' }))
    // focus restored to the element that opened the trap
    expect(document.activeElement).toBe(opener)
  })

  it('wraps Tab from the last tabbable back to the first', () => {
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'open' }))
    const close = screen.getByRole('button', { name: 'close' })
    close.focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'first' }))
  })
})
