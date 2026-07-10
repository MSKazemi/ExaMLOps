import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { StatusPill } from './status-pill'
import { HEALTH_STATUSES, SEVERITIES, statusMeta, normalizeHealth } from '@/lib/status'

describe('StatusPill', () => {
  it('renders the canonical label for every health status', () => {
    for (const s of HEALTH_STATUSES) {
      const { unmount } = render(<StatusPill status={s} />)
      expect(screen.getByText(statusMeta(s).label)).toBeInTheDocument()
      unmount()
    }
  })

  it('renders an icon alongside the label so status is never conveyed by colour alone', () => {
    const { container } = render(<StatusPill status="failed" />)
    expect(screen.getByText('Failed')).toBeInTheDocument()
    // an <svg> icon accompanies the colour — the non-colour cue required for colourblind safety
    expect(container.querySelector('svg')).toBeTruthy()
  })

  it('exposes an accessible status role and label', () => {
    render(<StatusPill status="healthy" />)
    expect(screen.getByRole('status')).toHaveAttribute('aria-label', 'Healthy')
  })

  it('honours a custom label override while keeping the status data attribute', () => {
    render(<StatusPill status="degraded" label="2 nodes draining" />)
    const el = screen.getByText('2 nodes draining')
    expect(el).toBeInTheDocument()
    expect(el).toHaveAttribute('data-status', 'degraded')
  })

  it('can hide the icon but still renders the text label', () => {
    const { container } = render(<StatusPill status="pending" showIcon={false} />)
    expect(container.querySelector('svg')).toBeNull()
    expect(screen.getByText('Pending')).toBeInTheDocument()
  })

  it('falls back to Unknown for an unrecognised status string', () => {
    render(<StatusPill status="banana" />)
    expect(screen.getByText('Unknown')).toBeInTheDocument()
  })

  it('maps every severity to a labelled pill', () => {
    for (const s of SEVERITIES) {
      const { unmount } = render(<StatusPill status={s} />)
      expect(screen.getByText(statusMeta(s).label)).toBeInTheDocument()
      unmount()
    }
  })
})

describe('normalizeHealth', () => {
  it('maps common backend synonyms onto canonical statuses', () => {
    expect(normalizeHealth('RUNNING')).toBe('healthy')
    expect(normalizeHealth('ok')).toBe('healthy')
    expect(normalizeHealth('stale')).toBe('degraded')
    expect(normalizeHealth('Down')).toBe('failed')
    expect(normalizeHealth('queued')).toBe('pending')
    expect(normalizeHealth('')).toBe('unknown')
    expect(normalizeHealth(null)).toBe('unknown')
    expect(normalizeHealth('something-else')).toBe('unknown')
  })
})
