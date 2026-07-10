import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { HelpDrawer } from './HelpDrawer'

describe('HelpDrawer', () => {
  it('is collapsed to a launcher until opened', () => {
    render(<HelpDrawer />)
    expect(screen.getByRole('button', { name: 'Open help' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('opens on the ? key and shows the glossary', () => {
    render(<HelpDrawer />)
    fireEvent.keyDown(window, { key: '?' })
    expect(screen.getByRole('dialog', { name: 'Help & glossary' })).toBeInTheDocument()
    expect(screen.getByText('Drift')).toBeInTheDocument()
  })

  it('filters the glossary via search', () => {
    render(<HelpDrawer />)
    fireEvent.click(screen.getByRole('button', { name: 'Open help' }))
    fireEvent.change(screen.getByLabelText('Search glossary'), { target: { value: 'canary' } })
    expect(screen.getByText('Canary')).toBeInTheDocument()
    expect(screen.queryByText('Drift')).not.toBeInTheDocument()
  })
})
