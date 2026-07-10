import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SkipLink } from './SkipLink'

describe('SkipLink', () => {
  it('links to the main landmark', () => {
    render(<SkipLink />)
    const link = screen.getByRole('link', { name: 'Skip to main content' })
    expect(link).toHaveAttribute('href', '#main')
  })

  it('is visually hidden until focused (sr-only)', () => {
    render(<SkipLink />)
    expect(screen.getByRole('link')).toHaveClass('sr-only')
  })
})
