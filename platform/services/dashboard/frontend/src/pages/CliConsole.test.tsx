import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { clearAuth, setAuth } from '@/lib/auth'
import type { CliCatalog, CliCommand } from '@/lib/cli'
import { CliConsole } from './CliConsole'

const command = (over: Partial<CliCommand>): CliCommand => ({
  path: 'x',
  group: 'x',
  panel: 'Projects & Workspaces',
  help: '',
  short_help: '',
  examples: [],
  tier: 'read',
  reason: null,
  params: [],
  forced_args: [],
  ...over,
})

const CATALOG: CliCatalog = {
  commands: [
    command({ path: 'project list', group: 'project', help: 'List projects.', examples: [{ cmd: 'exa project list', comment: '' }] }),
    command({
      path: 'project create',
      group: 'project',
      tier: 'admin',
      params: [{ name: 'name', kind: 'argument', type: 'string', required: true }],
    }),
    command({
      path: 'project delete',
      group: 'project',
      tier: 'destructive',
      params: [{ name: 'name', kind: 'argument', type: 'string', required: true }],
    }),
    command({
      path: 'stack down',
      group: 'stack',
      panel: 'Platform & Integrations',
      tier: 'cli_only',
      reason: 'Stops the host stack, including this dashboard. Use the Services console.',
    }),
  ],
  total: 4,
  tiers: { read: 1, admin: 1, destructive: 1, cli_only: 1 },
  unclassified: [],
  panels: ['Projects & Workspaces', 'Platform & Integrations'],
}

let caps: string[] = []
const apiFetch = vi.fn((path: string, opts?: { method?: string; body?: string }) => {
  if (path === '/api/v1/cli/catalog') return Promise.resolve(CATALOG)
  if (path === '/api/v1/cli/runs' && opts?.method === 'POST') {
    return Promise.resolve({ id: 'r1', status: 'running', command: 'project list', display: 'exa --json project list' })
  }
  if (path === '/api/v1/cli/runs') return Promise.resolve({ runs: [] })
  if (path.startsWith('/api/v1/cli/runs/r1')) {
    return Promise.resolve({
      id: 'r1',
      command: 'project list',
      display: 'exa --json project list',
      tier: 'read',
      format: 'json',
      actor: 'dashboard:viewer@x',
      status: 'succeeded',
      exit_code: 0,
      created_at: 0,
      started_at: 0,
      finished_at: 1,
      duration_ms: 420,
      error: null,
      args: {},
      stdout: '[{"name":"research"}]',
      stderr: '',
      truncated: false,
      parsed: [{ name: 'research', status: 'ACTIVE' }],
      files: [],
    })
  }
  if (path === '/api/v1/cli/workspace') return Promise.resolve({ files: [] })
  return Promise.resolve({})
})

vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string, { method?: string; body?: string }])),
  useMe: () => ({ data: { capabilities: caps, tenant: 'default' } }),
}))

function renderAt(url: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <CliConsole />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const posted = () =>
  apiFetch.mock.calls.filter((c) => c[0] === '/api/v1/cli/runs' && c[1]?.method === 'POST').map((c) => JSON.parse(c[1]!.body!))

describe('CLI Console', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })
  afterEach(() => clearAuth())

  it('lists every command with its coverage summary', async () => {
    caps = ['view', 'cli.run']
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderAt('/platform/cli')
    expect(await screen.findByText('project create')).toBeInTheDocument()
    expect(screen.getByText('stack down')).toBeInTheDocument()
    expect(screen.getByLabelText('Command coverage')).toHaveTextContent('4 commands')
    expect(screen.getByLabelText('Command coverage')).toHaveTextContent('3 runnable here')
  })

  it('lets a viewer run a read command and renders the JSON as a table', async () => {
    caps = ['view', 'cli.run']
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderAt('/platform/cli?cmd=project%20list')
    fireEvent.click(await screen.findByRole('button', { name: /Run/ }))
    await waitFor(() => expect(posted()).toEqual([{ command: 'project list', args: {}, format: 'json' }]))
    expect(await screen.findByText('research')).toBeInTheDocument()
    expect(screen.getByText('Succeeded')).toBeInTheDocument()
  })

  it('disables a write for a viewer and says why', async () => {
    caps = ['view', 'cli.run']
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderAt('/platform/cli?cmd=project%20create')
    const run = await screen.findByRole('button', { name: /Run/ })
    expect(run).toBeDisabled()
    expect(screen.getAllByText('Requires the admin role.').length).toBeGreaterThan(0)
  })

  it('makes an admin type a destructive command back before it runs', async () => {
    caps = ['view', 'cli.run', 'cli.write']
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderAt('/platform/cli?cmd=project%20delete')
    fireEvent.change(await screen.findByLabelText(/NAME/), { target: { value: 'old' } })
    const run = screen.getByRole('button', { name: /Run/ })
    expect(run).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Type the command to confirm'), { target: { value: 'project delete' } })
    expect(run).not.toBeDisabled()
    fireEvent.click(run)
    await waitFor(() =>
      expect(posted()).toEqual([{ command: 'project delete', args: { name: 'old' }, format: 'json', confirm: 'project delete' }]),
    )
  })

  it('shows why a cli_only command cannot run here, with no Run button', async () => {
    caps = ['view', 'cli.run', 'cli.write']
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderAt('/platform/cli?cmd=stack%20down')
    expect(await screen.findByText(/Use the Services console/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Run/ })).not.toBeInTheDocument()
  })

  it('narrows the list when opened from a console link', async () => {
    caps = ['view', 'cli.run']
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderAt('/platform/cli?filter=stack')
    expect(await screen.findByText('stack down')).toBeInTheDocument()
    expect(screen.queryByText('project list')).not.toBeInTheDocument()
  })
})
