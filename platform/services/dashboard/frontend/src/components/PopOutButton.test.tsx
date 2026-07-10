import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { PopOutButton } from './PopOutButton'

describe('PopOutButton', () => {
  it('opens the given url in a detached window (R5)', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue({} as Window)
    render(<PopOutButton url="http://grafana/panel" label="Pop out logs" />)
    fireEvent.click(screen.getByRole('button', { name: 'Pop out logs' }))
    expect(open).toHaveBeenCalledWith('http://grafana/panel', 'examlops-panel', expect.stringContaining('popup'))
    open.mockRestore()
  })

  it('is hidden from print output', () => {
    render(<PopOutButton url="http://x" />)
    expect(screen.getByRole('button')).toHaveClass('no-print')
  })
})
