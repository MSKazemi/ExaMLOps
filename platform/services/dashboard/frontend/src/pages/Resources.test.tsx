import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { clearAuth, setAuth } from '@/lib/auth'
import type { CliCatalog, CliCommand } from '@/lib/cli'
import { Resources } from './Resources'

const cmd = (over: Partial<CliCommand>): CliCommand => ({
  path: 'x',
  group: 'project',
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

const nameArg = { name: 'name', kind: 'argument' as const, type: 'string' as const, required: true }

const CATALOG: CliCatalog = {
  commands: [
    cmd({ path: 'project list', params: [{ name: 'status', kind: 'option', type: 'string', opts: ['--status'] }] }),
    cmd({ path: 'project create', tier: 'admin', params: [nameArg, { name: 'description', kind: 'option', type: 'string', opts: ['--description'] }] }),
    cmd({ path: 'project show', params: [nameArg] }),
    cmd({ path: 'project delete', tier: 'destructive', params: [nameArg, { name: 'yes', kind: 'option', type: 'bool', flag: true, opts: ['--yes'], implied: true }] }),
  ],
  total: 4,
  tiers: { read: 2, admin: 1, destructive: 1, cli_only: 0 },
  unclassified: [],
  panels: ['Projects & Workspaces'],
  resources: [
    {
      id: 'project',
      title: 'Projects',
      description: 'Workspaces.',
      panel: 'Projects & Workspaces',
      list: 'project list',
      key: 'name',
      rows_key: null,
      create: 'project create',
      show: 'project show',
      edit: [],
      delete: 'project delete',
      actions: [],
      bindings: { 'project show': { name: 'name' }, 'project delete': { name: 'name' } },
      columns: ['name', 'status'],
      tiers: { 'project list': 'read', 'project create': 'admin', 'project show': 'read', 'project delete': 'destructive' },
    },
  ],
  resource_coverage: { resources: 1, commands: 4, runnable: 4 },
}

let caps: string[] = []
let runs: Record<string, { command: string; args: Record<string, unknown> }> = {}
let seq = 0

const apiFetch = vi.fn((path: string, opts?: { method?: string; body?: string }) => {
  if (path === '/api/v1/cli/catalog') return Promise.resolve(CATALOG)
  if (path === '/api/v1/cli/workspace') return Promise.resolve({ files: [] })
  if (path === '/api/v1/cli/runs' && opts?.method === 'POST') {
    const body = JSON.parse(opts.body!)
    const id = `r${++seq}`
    runs[id] = body
    return Promise.resolve({ id, status: 'running', command: body.command, display: `exa ${body.command}` })
  }
  const m = path.match(/^\/api\/v1\/cli\/runs\/(r\d+)$/)
  if (m) {
    const run = runs[m[1]]
    const parsed =
      run.command === 'project list'
        ? [{ name: 'p1', status: 'ACTIVE' }, { name: 'p2', status: 'ARCHIVED' }]
        : run.command === 'project show'
          ? { name: 'p1', description: 'first' }
          : { ok: true }
    return Promise.resolve({
      id: m[1], command: run.command, display: `exa ${run.command}`, tier: 'read', format: 'json', actor: 'a',
      status: 'succeeded', exit_code: 0, created_at: 0, started_at: 0, finished_at: 0, duration_ms: 1, error: null,
      args: run.args, stdout: JSON.stringify(parsed), stderr: '', truncated: false, parsed, files: [],
    })
  }
  return Promise.resolve({})
})

vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string, { method?: string; body?: string }])),
  useMe: () => ({ data: { capabilities: caps, tenant: 'default' } }),
}))

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/platform/resources?r=project']}>
        <Resources />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const posts = () => Object.values(runs)
const as = (role: 'viewer' | 'admin') => {
  caps = role === 'admin' ? ['view', 'cli.run', 'cli.write'] : ['view', 'cli.run']
  setAuth({ token: 't', role, expiresAt: new Date(Date.now() + 3600_000).toISOString() })
}

describe('Resource Manager', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    runs = {}
    seq = 0
  })
  afterEach(() => clearAuth())

  it('lists a resource as a table, read-only for viewers', async () => {
    as('viewer')
    renderPage()
    expect(await screen.findByText('p2')).toBeInTheDocument()
    expect(posts()[0]).toMatchObject({ command: 'project list', args: {} })
    expect(screen.getByRole('button', { name: /New/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Delete p1' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'View p1' })).not.toBeDisabled()
    expect(screen.getByLabelText('Resource coverage')).toHaveTextContent('1 resources')
  })

  it('opens View pre-filled from the row and loads it straight away', async () => {
    as('viewer')
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'View p1' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByDisplayValue('p1')).toHaveAttribute('readonly')
    await waitFor(() => expect(posts().some((r) => r.command === 'project show' && r.args.name === 'p1')).toBe(true))
    expect(await within(dialog).findByText('first')).toBeInTheDocument()
  })

  it('creates an item from the New dialog and refreshes the table', async () => {
    as('admin')
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /New/ }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/NAME/), { target: { value: 'p3' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(posts().some((r) => r.command === 'project create' && r.args.name === 'p3')).toBe(true))
    await waitFor(() => expect(posts().filter((r) => r.command === 'project list').length).toBeGreaterThan(1))
  })

  it('deletes only after the command is typed back, with the row’s identity locked', async () => {
    as('admin')
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Delete p2' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByDisplayValue('p2')).toHaveAttribute('readonly')
    const del = within(dialog).getByRole('button', { name: 'Delete' })
    expect(del).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Type the command to confirm'), { target: { value: 'project delete' } })
    fireEvent.click(del)
    await waitFor(() =>
      expect(posts().find((r) => r.command === 'project delete')).toMatchObject({
        args: { name: 'p2' },
        confirm: 'project delete',
      }),
    )
  })

  it('closes the dialog on Escape', async () => {
    as('admin')
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /New/ }))
    await screen.findByRole('dialog')
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})
