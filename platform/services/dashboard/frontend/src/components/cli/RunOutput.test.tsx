import { afterEach, describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { CliRunDetail } from '@/lib/cli'
import { RunOutput } from './RunOutput'

function run(over: Partial<CliRunDetail>): CliRunDetail {
  return {
    id: 'r1',
    command: 'eval run',
    display: 'exa eval run JPCP',
    tier: 'admin',
    format: 'text',
    actor: 'dashboard:admin',
    status: 'running',
    exit_code: null,
    created_at: 0,
    started_at: 0,
    finished_at: null,
    duration_ms: 1200,
    error: null,
    args: {},
    stdout: '',
    stderr: '',
    truncated: false,
    parsed: null,
    files: [],
    ...over,
  } as CliRunDetail
}

// The run as the server reports it — seeded, and returned again by every poll.
function renderRun(r: CliRunDetail) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(r), { status: 200 }))),
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['cli', 'run', r.id], r)
  return render(
    <QueryClientProvider client={qc}>
      <RunOutput runId={r.id} canDownload />
    </QueryClientProvider>,
  )
}

afterEach(() => vi.unstubAllGlobals())

describe('RunOutput — a long run shows its output as it goes', () => {
  it('shows what a running text command has printed so far', () => {
    renderRun(run({ stdout: 'step 1 of 3: fetching\nstep 2 of 3: scoring\n' }))
    expect(screen.getByLabelText('Output so far')).toHaveTextContent('step 2 of 3: scoring')
    expect(screen.getByRole('button', { name: /Cancel/ })).toBeInTheDocument()
  })

  it('says it is waiting when nothing has been printed yet', () => {
    renderRun(run({}))
    expect(screen.getByText('Waiting for output…')).toBeInTheDocument()
  })

  it('explains that a JSON result arrives at the end, and shows its messages meanwhile', () => {
    renderRun(run({ format: 'json', stderr: 'warning: judge not calibrated\n' }))
    expect(screen.getByText(/result appears when the command finishes/)).toBeInTheDocument()
    expect(screen.getByLabelText('Output so far')).toHaveTextContent('judge not calibrated')
  })

  it('replaces the live view with the rendered result once the run is done', () => {
    renderRun(run({ status: 'succeeded', exit_code: 0, stdout: 'all done\n' }))
    expect(screen.queryByLabelText('Output so far')).not.toBeInTheDocument()
    expect(screen.getByText('all done')).toBeInTheDocument()
  })
})
