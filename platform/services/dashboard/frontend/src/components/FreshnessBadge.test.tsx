import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { FreshnessBadge } from './FreshnessBadge'

describe('FreshnessBadge', () => {
  it('renders CURRENT badge in green', () => {
    render(<FreshnessBadge status="current" />)
    const badge = screen.getByText('CURRENT')
    expect(badge).toBeTruthy()
    expect(badge.className).toMatch(/green|emerald/)
  })

  it('renders UPDATED badge in amber', () => {
    render(<FreshnessBadge status="stale" />)
    const badge = screen.getByText('UPDATED')
    expect(badge).toBeTruthy()
    expect(badge.className).toMatch(/amber/)
  })

  it('renders nothing for unknown status', () => {
    const { container } = render(<FreshnessBadge status="unknown" />)
    expect(container.firstChild).toBeNull()
  })

  it('shows stale_since in tooltip title', () => {
    render(<FreshnessBadge status="stale" staleSince="2026-05-21T10:00:00" />)
    const badge = screen.getByText('UPDATED')
    expect(badge.getAttribute('title')).toContain('2026-05-21')
  })
})
