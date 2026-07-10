import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { ConnectionBadge } from './connection-badge'
import type { ConnectionState } from '@/lib/realtime'

describe('ConnectionBadge', () => {
  it('renders a distinct label for every connection state (never colour-only)', () => {
    const cases: Record<ConnectionState, string> = {
      live: 'Live',
      reconnecting: 'Reconnecting…',
      polling: 'Polling (offline)',
    }
    for (const [state, label] of Object.entries(cases)) {
      const { unmount } = render(<ConnectionBadge state={state as ConnectionState} />)
      expect(screen.getByText(label)).toBeInTheDocument()
      unmount()
    }
  })

  it('exposes an accessible status role', () => {
    render(<ConnectionBadge state="live" />)
    expect(screen.getByRole('status')).toHaveAttribute('aria-label', 'Live')
  })
})
