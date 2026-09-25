import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { setAuth, clearAuth } from '@/lib/auth'
import type { ProjectDetail as ProjectDetailT } from '@/lib/projects'
import { ProjectDetail } from './ProjectDetail'

// ADR 0093 decision 3: the Storage / Connections / Pipelines panels of the project detail page,
// rendered from the anatomy the BFF returns — including the live-hydrated pipeline surfaces
// (ADR 0092) and the partial-view notice. Asserts what the operator sees, not the helpers.

const BASE: ProjectDetailT = {
  name: 'climate',
  description: 'climate team',
  status: 'ACTIVE',
  namespace: 'examlops-climate',
  quota: { cpuLimit: 4, memoryLimitGb: 8, storageGb: 100, gpuLimit: 2 },
  resources: { model: ['JPCP'] },
  members: [],
  budget: null,
  consumption: { gpu_hours: 4.5, cost_usd: 18 },
  createdAt: '2026-09-01T00:00:00',
  createdBy: 'alice',
  storage: { bucket: 'examlops-projects', prefix: 'climate/', quotaGb: 100, usedBytes: 12_400_000_000, connectionRef: null },
  connections: [],
  pipelines: {
    prefect: {
      deployments: ['examlops-jpcp-nightly'],
      schedule: '0 2 * * *',
      lastRunAt: '2026-09-24T02:00:00Z',
      lastRunState: 'FAILED',
      workPool: 'hpc',
      storagePrefix: 's3://examlops-projects/climate/',
      status: 'degraded',
      source: 'live',
    },
    rayserve: {
      models: ['JPCP', 'FDATA'],
      traffic: { JPCP: { Production: 90, Canary: 10 } },
      served: ['JPCP'],
      unserved: ['FDATA'],
      aliases: { JPCP: ['Canary', 'Production'] },
      health: { JPCP: 'ok' },
      status: 'degraded',
      source: 'live',
    },
  },
}

let detail: ProjectDetailT = BASE
const CONNECTIONS = [
  { name: 'raw-s3', project: 'climate', kind: 's3', config: {}, hasSecret: true, createdAt: '', createdBy: 'alice' },
]

const apiFetch = vi.fn((path: string) => {
  if (path === '/api/v1/projects/climate') return Promise.resolve(detail)
  if (path.startsWith('/api/v1/connections/kinds')) return Promise.resolve({ kinds: ['s3'] })
  if (path.startsWith('/api/v1/connections')) return Promise.resolve(CONNECTIONS)
  return Promise.resolve([])
})
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string])),
  useMe: () => ({ data: undefined }),
}))

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/platform/projects/climate']}>
        <Routes>
          <Route path="/platform/projects/:name" element={<ProjectDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ProjectDetail anatomy panels (ADR 0093)', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    detail = BASE
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
  })
  afterEach(() => clearAuth())

  it('renders the storage location and usage', async () => {
    renderPage()
    expect(await screen.findByText(/s3:\/\/examlops-projects\/climate\/ · 12\.40 GB used \(12% of quota\)/)).toBeInTheDocument()
  })

  it('lists connections with their secret flag, never a value', async () => {
    renderPage()
    expect(await screen.findByText('raw-s3')).toBeInTheDocument()
    expect(screen.queryByText(/SEKRET|secret_ref/)).not.toBeInTheDocument()
  })

  it('shows the live Prefect surface: deployments, pool, last run and artifact prefix', async () => {
    renderPage()
    const card = await screen.findByTestId('pipeline-prefect')
    expect(within(card).getByText('live')).toBeInTheDocument()
    expect(within(card).getByText('examlops-jpcp-nightly')).toBeInTheDocument()
    expect(within(card).getByText(/pool hpc/)).toBeInTheDocument()
    expect(within(card).getByText(/last run: FAILED · 2026-09-24 02:00:00/)).toBeInTheDocument()
    expect(within(card).getByText(/artifacts → s3:\/\/examlops-projects\/climate\//)).toBeInTheDocument()
    expect(within(card).getByText('degraded')).toBeInTheDocument()
  })

  it('shows served aliases and the models the serve app is not serving', async () => {
    renderPage()
    const card = await screen.findByTestId('pipeline-rayserve')
    expect(within(card).getByText('1 served model(s)')).toBeInTheDocument()
    expect(within(card).getByText(/JPCP: Canary \/ Production/)).toBeInTheDocument()
    expect(within(card).getByText('not served: FDATA')).toBeInTheDocument()
  })

  it('labels a registry fallback and says the service was unreachable', async () => {
    detail = {
      ...BASE,
      pipelines: {
        prefect: { deployments: ['examlops-jpcp'], schedule: null, lastRunAt: null, status: 'healthy', source: 'registry', liveError: 'HTTP 503' },
        rayserve: null,
      },
    }
    renderPage()
    const card = await screen.findByTestId('pipeline-prefect')
    expect(within(card).getByText('registry')).toBeInTheDocument()
    expect(within(card).getByText(/Prefect unreachable/)).toBeInTheDocument()
    expect(screen.queryByTestId('pipeline-rayserve')).not.toBeInTheDocument()
  })

  it('counts registry members, not the empty live `served` list, when no serve app answered', async () => {
    // The exact shape routers/projects.py `_pipelines_view` emits for a registry-only read:
    // `served: []` is always present, so a `served ?? models` fallback never falls back.
    detail = {
      ...BASE,
      pipelines: {
        prefect: null,
        rayserve: {
          models: ['JPCP', 'FDATA'], traffic: {}, served: [], unserved: [], aliases: {}, health: {},
          status: 'unknown', source: 'registry', liveError: null,
        },
      },
    }
    renderPage()
    const card = await screen.findByTestId('pipeline-rayserve')
    expect(within(card).getByText('2 member model(s)')).toBeInTheDocument()
    expect(within(card).queryByText(/0 served/)).not.toBeInTheDocument()
  })

  it('names the sections a partial BFF view could not load', async () => {
    detail = { ...BASE, _partial: ['consumption', 'pipelines'] }
    renderPage()
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Some sections could not be loaded: consumption, pipelines',
    )
  })

  it('shows no partial notice for a complete view', async () => {
    renderPage()
    await screen.findByTestId('pipeline-prefect')
    expect(screen.queryByText(/could not be loaded/)).not.toBeInTheDocument()
  })
})
