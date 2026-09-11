import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { CliFlagGate } from './CliFlagGate'

// The server's decision, as `GET /api/v1/flags` would have delivered it (F25 R2).
function renderGate(cliConsole: boolean | undefined, quiet = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (cliConsole !== undefined) qc.setQueryData(['flags', 'decisions'], { flags: { cliConsole } })
  return render(
    <QueryClientProvider client={qc}>
      <CliFlagGate quiet={quiet}>
        <p>console body</p>
      </CliFlagGate>
    </QueryClientProvider>,
  )
}

describe('CliFlagGate — the cliConsole kill switch (ADR 0119)', () => {
  it('renders the console while the server says the flag is on', () => {
    renderGate(true)
    expect(screen.getByText('console body')).toBeInTheDocument()
  })

  it('says the console is switched off instead of rendering it', () => {
    renderGate(false)
    expect(screen.queryByText('console body')).not.toBeInTheDocument()
    // Still reads as the page it replaces: its own frame and heading, not a bare notice.
    expect(screen.getByRole('heading', { level: 1, name: 'CLI Console' })).toBeInTheDocument()
    expect(screen.getByText(/CLI Console is switched off/i)).toBeInTheDocument()
    expect(screen.getByText(/Platform → Flags/)).toBeInTheDocument()
  })

  it('a quiet (embedded) surface simply disappears', () => {
    const { container } = renderGate(false, true)
    expect(container).toBeEmptyDOMElement()
  })

  it('falls back to the client default (on) before the server has answered', () => {
    renderGate(undefined)
    expect(screen.getByText('console body')).toBeInTheDocument()
  })
})
