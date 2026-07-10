import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { CopilotAnswer, CopilotPanel } from './CopilotPanel'
import type { CopilotResponse } from '@/lib/copilot'

const RESPONSE: CopilotResponse = {
  answer: 'jpcp is drifting because inputs shifted.',
  hitl_required: false,
  proposals: [
    { command: 'exa drift status', requiresApproval: false },
    { command: 'exa retrain jpcp --dataset PM100', requiresApproval: true },
  ],
  trace: [{ kind: 'tool', name: 'drift', detail: 'queried jpcp' }],
}

describe('CopilotAnswer', () => {
  it('renders the answer, proposals with gate badges, and trace', () => {
    render(<CopilotAnswer response={RESPONSE} />)
    expect(screen.getByText(/inputs shifted/)).toBeInTheDocument()
    expect(screen.getByText('exa drift status')).toBeInTheDocument()
    expect(screen.getByText('Read-only')).toBeInTheDocument()
    expect(screen.getByText('Needs approval')).toBeInTheDocument()
    // trace is collapsible
    expect(screen.getByText(/Agent trace \(1\)/)).toBeInTheDocument()
  })

  it('shows a degraded banner when the agent was unavailable', () => {
    render(<CopilotAnswer response={{ ...RESPONSE, _partial: ['agent'] }} />)
    expect(screen.getByText(/Agent unavailable/)).toBeInTheDocument()
  })

  it('offers copy — never a run — button for each proposal (R5: propose-only)', () => {
    render(<CopilotAnswer response={RESPONSE} />)
    expect(screen.getByRole('button', { name: 'Copy exa drift status' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /run/i })).not.toBeInTheDocument()
  })
})

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/models/jpcp']}>
        <CopilotPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('CopilotPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('is collapsed to a launcher button by default', () => {
    renderPanel()
    expect(screen.getByRole('button', { name: 'Open copilot' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Copilot' })).not.toBeInTheDocument()
  })

  it('opens the drawer and posts a grounded question, rendering the answer', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(RESPONSE), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: 'Open copilot' }))
    expect(screen.getByRole('dialog', { name: 'Copilot' })).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Ask the copilot'), { target: { value: 'why drift?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(screen.getByText(/inputs shifted/)).toBeInTheDocument())
    // the grounded page context was sent to the BFF
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
    expect(body.context.page).toBe('/models/jpcp')
    expect(body.context.entity).toEqual({ type: 'models', id: 'jpcp' })
  })
})
