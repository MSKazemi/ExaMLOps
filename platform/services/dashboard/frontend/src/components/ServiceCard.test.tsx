/**
 * A service card must never invent a verdict.
 *
 * `/api/health` used to publish `slurm: ok` whenever the platform was in mock mode — a green tick
 * for a scheduler that is not involved — and `down` otherwise, which pinned the whole payload at
 * `degraded` forever on any real-scheduler deployment. The backend now answers `unknown` with a
 * note for the entries it does not probe, and these tests pin the two properties that make that
 * honest at the surface the operator actually looks at: `unknown` reads as *not measured* rather
 * than as a failure, and a status word the frontend has never heard of cannot crash the page.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ServiceCard } from './ServiceCard'
import type { ServiceStatus } from '@/lib/api'

describe('ServiceCard', () => {
  it('shows an unmeasured service as not measured, not as offline', () => {
    render(<ServiceCard name="Slurm Adapter" url="" status="unknown" />)
    expect(screen.getByText('Not measured')).toBeTruthy()
    expect(screen.queryByText('Offline')).toBeNull()
    expect(screen.queryByText('Online')).toBeNull()
  })

  it('puts the reason it is unmeasured where the operator can read it', () => {
    const note = 'EXAMLOPS_SLURM_MODE=mock — training runs inline, so there is no scheduler to probe.'
    render(<ServiceCard name="Slurm Adapter" url="" status="unknown" note={note} />)
    expect(screen.getByText('Not measured').closest('[title]')?.getAttribute('title')).toBe(note)
  })

  it('still renders the three measured verdicts', () => {
    for (const [status, label] of [['ok', 'Online'], ['degraded', 'Degraded'], ['down', 'Offline']] as const) {
      const { unmount } = render(<ServiceCard name="MLflow" url="http://x" status={status} />)
      expect(screen.getByText(label)).toBeTruthy()
      unmount()
    }
  })

  it('does not crash on a status the backend has not taught it yet', () => {
    // The status is API data, so the union type proves nothing about what actually arrives.
    // Before the fallback, `STATUS_CONFIG[status]` was `undefined` here and reading `.bg` off it
    // threw inside render — one new word in a backend payload would blank the Overview page.
    const rogue = 'starting' as ServiceStatus
    render(<ServiceCard name="Ray Serve" url="http://x" status={rogue} />)
    expect(screen.getByText('Not measured')).toBeTruthy()
  })
})
