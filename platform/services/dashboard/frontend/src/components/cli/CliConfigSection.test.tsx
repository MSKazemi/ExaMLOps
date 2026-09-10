import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { clearAuth, setAuth } from '@/lib/auth'
import { CliConfigSection } from './CliConfigSection'

let caps: string[] = []
let runs: Record<string, { command: string; args: Record<string, unknown>; context?: string; confirm?: string }> = {}
let seq = 0

const ENV = (context?: string) => ({
  active_context: 'prod',
  config_file: '/state/config.toml',
  settings: [
    { key: 'control_plane_url', value: context === 'stg' ? 'http://stg:18002' : 'http://prod:18002', source: context === 'stg' ? 'context:stg' : 'context:prod' },
    { key: 'mlflow_url', value: 'http://mlflow:5000', source: 'file' },
    { key: 'agent_url', value: 'http://agent:18004', source: 'env:AGENT_URL' },
    { key: 'agent_token', value: '(unset)', source: 'default' },
  ],
})

const apiFetch = vi.fn((path: string, opts?: { method?: string; body?: string }) => {
  if (path === '/api/v1/cli/runs' && opts?.method === 'POST') {
    const body = JSON.parse(opts.body!)
    const id = `r${++seq}`
    runs[id] = body
    return Promise.resolve({ id, status: 'running', command: body.command })
  }
  const m = path.match(/^\/api\/v1\/cli\/runs\/(r\d+)$/)
  if (m) {
    const run = runs[m[1]]
    const parsed =
      run.command === 'config contexts'
        ? { contexts: ['prod', 'stg'], active: 'prod' }
        : run.command === 'env'
          ? run.args.validate
            ? [{ level: 'warn', key: 'CONTROL_PLANE_TOKEN', message: 'placeholder' }]
            : ENV(run.context)
          : { ok: true, message: `${run.command} done` }
    return Promise.resolve({
      id: m[1], command: run.command, display: '', tier: 'read', format: 'json', actor: 'a', status: 'succeeded',
      exit_code: 0, created_at: 0, started_at: 0, finished_at: 0, duration_ms: 1, error: null, args: run.args,
      stdout: '', stderr: '', truncated: false, parsed, files: [],
    })
  }
  return Promise.resolve({})
})

vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string, { method?: string; body?: string }])),
  useMe: () => ({ data: { capabilities: caps, tenant: 'default' } }),
}))

function renderIt() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CliConfigSection />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const posted = (command: string) => Object.values(runs).filter((r) => r.command === command)
const as = (role: 'viewer' | 'admin') => {
  caps = role === 'admin' ? ['view', 'cli.run', 'cli.write'] : ['view', 'cli.run']
  setAuth({ token: 't', role, expiresAt: new Date(Date.now() + 3600_000).toISOString() })
}

describe('exa CLI configuration', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    runs = {}
    seq = 0
  })
  afterEach(() => clearAuth())

  it('shows every effective setting with where it comes from', async () => {
    as('viewer')
    renderIt()
    expect(await screen.findByText('http://mlflow:5000')).toBeInTheDocument()
    expect(screen.getByText('env AGENT_URL')).toBeInTheDocument()
    expect(screen.getByText('base config')).toBeInTheDocument()
    expect(screen.getByText(/Context prod is active/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit mlflow_url' })).toBeDisabled() // viewer
  })

  it('locks a value set by the environment, since a file value would lose to it', async () => {
    as('admin')
    renderIt()
    const edit = await screen.findByRole('button', { name: 'Edit agent_url' })
    expect(edit).toBeDisabled()
    expect(edit.getAttribute('title')).toMatch(/AGENT_URL.*environment/)
    expect(screen.getByRole('button', { name: 'Edit mlflow_url' })).not.toBeDisabled()
  })

  it('edits the base value with exa config set', async () => {
    as('admin')
    renderIt()
    fireEvent.click(await screen.findByRole('button', { name: 'Edit mlflow_url' }))
    fireEvent.change(screen.getByLabelText('New value for mlflow_url'), { target: { value: 'http://new:5000' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(posted('config set')[0]?.args).toEqual({ key: 'mlflow_url', value: 'http://new:5000' }))
  })

  it('edits inside a context when one is selected, and resets its override', async () => {
    as('admin')
    renderIt()
    fireEvent.click(await screen.findByRole('button', { name: 'stg' }))
    expect(await screen.findByText('http://stg:18002')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reset control_plane_url' }))
    await waitFor(() => expect(posted('config unset')[0]?.args).toEqual({ key: 'control_plane_url', context: 'stg' }))
  })

  it('creates a context from its first override, without activating it', async () => {
    as('admin')
    renderIt()
    fireEvent.click(await screen.findByRole('button', { name: /New context/ }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Context name'), { target: { value: 'dr' } })
    fireEvent.change(within(dialog).getByLabelText('Value'), { target: { value: 'http://dr:18002' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }))
    await waitFor(() =>
      expect(posted('config set')[0]?.args).toEqual({ key: 'control_plane_url', value: 'http://dr:18002', context: 'dr' }),
    )
    expect(posted('config use')).toHaveLength(0)
  })

  it('deletes a context only after its name is typed', async () => {
    as('admin')
    renderIt()
    fireEvent.click(await screen.findByRole('button', { name: 'Delete context stg' }))
    const dialog = await screen.findByRole('dialog')
    const del = within(dialog).getByRole('button', { name: 'Delete' })
    expect(del).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Type the context name to confirm'), { target: { value: 'stg' } })
    fireEvent.click(del)
    await waitFor(() =>
      expect(posted('config delete-context')[0]).toMatchObject({ args: { name: 'stg' }, confirm: 'config delete-context' }),
    )
  })

  it('switches the active context, and back to base with --clear', async () => {
    as('admin')
    renderIt()
    await screen.findByText('http://mlflow:5000')
    const stgChip = screen.getByRole('button', { name: 'stg' }).parentElement!
    fireEvent.click(within(stgChip).getByRole('button', { name: 'use' }))
    await waitFor(() => expect(posted('config use')[0]?.args).toEqual({ name: 'stg' }))
    const baseChip = screen.getByRole('button', { name: 'base' }).parentElement!
    fireEvent.click(within(baseChip).getByRole('button', { name: 'use' }))
    await waitFor(() => expect(posted('config use')[1]?.args).toEqual({ clear: true }))
  })

  it('runs validation and lists the findings', async () => {
    as('viewer')
    renderIt()
    fireEvent.click(await screen.findByRole('button', { name: /Validate/ }))
    expect(await screen.findByText('placeholder')).toBeInTheDocument()
  })
})
