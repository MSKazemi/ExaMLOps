import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AgentRuns } from './AgentRuns'

const SESSION = {
  session_id: 'sess-a',
  tenant: 'default',
  agent: 'skipper',
  model: 'm',
  steps: 2,
  tool_calls: 2,
  errors: 1,
  input_tokens: 0,
  output_tokens: 0,
  cost_usd: 0.0123,
  status: 'anomaly',
  anomalies: ['loop'],
  started_at: '2026-09-20 10:00:00',
  ended_at: '2026-09-20 10:00:05',
}

const apiFetch = vi.fn((path: string) => {
  if (path.startsWith('/api/agentops/sessions/sess-a'))
    return Promise.resolve({
      session: SESSION,
      steps: [
        { step: 0, tool: 'recall_memory', args_digest: 'abcdef0123456789', ok: 1, error: null, latency_ms: 12, ts: 't' },
        { step: 1, tool: 'platform_status', args_digest: null, ok: 0, error: 'control plane down', latency_ms: 30, ts: 't' },
      ],
    })
  if (path.startsWith('/api/agentops/sessions')) return Promise.resolve([SESSION])
  if (path.startsWith('/api/agentops/tools'))
    return Promise.resolve([{ tool: 'recall_memory', calls: 4, errors: 1, success_rate: 0.75, avg_latency_ms: 11 }])
  if (path.startsWith('/api/agentops/breaker'))
    return Promise.resolve([{ ts: 't1', event: 'warning', target: 'loop_warning', details: { detail: 'tool x repeated 2x' } }])
  return Promise.resolve({})
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string])),
  useMe: () => ({ data: undefined }),
}))

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AgentRuns />
    </QueryClientProvider>,
  )
}

describe('Agent runs console', () => {
  beforeEach(() => {
    apiFetch.mockClear()
  })

  it('lists sessions, tool success and breaker events read-only', async () => {
    renderPage()
    expect(await screen.findByText('sess-a')).toBeInTheDocument()
    expect(screen.getByText('loop')).toBeInTheDocument()
    expect(await screen.findByText(/75% of 4 calls succeeded/)).toBeInTheDocument()
    expect(await screen.findByText('loop_warning')).toBeInTheDocument()
    // read-only: no mutating request was made
    for (const call of apiFetch.mock.calls) expect(call.length).toBe(1)
  })

  it('replays a session with its steps and the redacted digest, never raw arguments', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Replay sess-a' }))
    await waitFor(() => expect(screen.getByText('platform_status')).toBeInTheDocument())
    expect(screen.getByText('control plane down')).toBeInTheDocument()
    expect(screen.getByText('args abcdef0123456789')).toBeInTheDocument()
  })
})
