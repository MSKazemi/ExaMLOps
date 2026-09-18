import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MlopsConsole } from './MlopsConsole'

// A read that fails must not be rendered as a statement about where live inference traffic goes.
// Two readers, two different lies, one cause (the panel binds `data` and drops `error`):
//   - the viewer is told "No traffic split set (100% Production by default)";
//   - the admin, who selected a *second* model, keeps the *first* model's weights in the editor
//     at a valid Σ=100, so one click writes one model's split onto another.

const OK_RULES = { model: 'JPCP', rules: { Production: 90, Canary: 10 } }

// The promotion panel is a sibling on the same column; give it something real so this file
// exercises the traffic panel and nothing else.
const PROMOTION = {
  model: 'JPCP',
  mlflowName: 'jpcp',
  policy: {
    allow: true,
    reasons: [],
    metric: 'rmse',
    operator: '<',
    threshold: 5,
    fromAlias: 'Staging',
    toAlias: 'Production',
  },
  eval: { state: 'no_gate', pass: null, reason: '', metrics: [], lastReport: null },
  approval: { required: false, state: 'none' },
  allowed: true,
}

let admin = false
vi.mock('@/lib/auth', () => ({ isAdmin: () => admin }))

const row = (name: string, mlflowName: string) => ({
  name,
  mlflowName,
  version: 18,
  stage: 'Staging',
  health: 'ok',
  freshness: null,
  governed: true,
})

vi.mock('@/lib/api', () => ({
  apiFetch: (url: string): Promise<unknown> => {
    if (url.includes('/mlops/registry'))
      return Promise.resolve({ registry: { rows: [row('JPCP', 'jpcp'), row('MACK', 'mack')], count: 2 } })
    if (url.includes('/mlops/promotion/')) return Promise.resolve({ promotion: PROMOTION })
    // The panel is keyed by the MLflow name (lowercase), not the display name.
    // JPCP's split reads fine; MACK's read fails — the datastore is refusing.
    if (url.includes('/traffic-rules/jpcp')) return Promise.resolve(OK_RULES)
    if (url.includes('/traffic-rules/mack')) return Promise.reject(new Error('traffic rules unavailable'))
    return Promise.resolve({})
  },
}))

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MlopsConsole />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  admin = false
})

describe('MLOps console — a failed traffic read is not a traffic statement', () => {
  it('does not tell a viewer the model runs 100% Production when the read failed', async () => {
    renderPage()
    fireEvent.click(await screen.findByText('MACK'))

    // The settled error state, not the heading (which renders before the query resolves).
    expect(await screen.findByText(/could not be read/i)).toBeInTheDocument()
    expect(screen.queryByText(/100% Production by default/)).not.toBeInTheDocument()
  })

  it('does not leave one model\'s weights armed in another model\'s editor', async () => {
    admin = true
    renderPage()

    // Load JPCP's real split: Production 90 / Canary 10.
    fireEvent.click(await screen.findByText('JPCP'))
    await waitFor(() => expect(screen.getByDisplayValue('90')).toBeInTheDocument())

    // Switch to MACK, whose read fails. JPCP's numbers must not survive into MACK's editor,
    // where Σ is a valid 100 and "Set split" would write them to MACK.
    fireEvent.click(screen.getByText('MACK'))
    expect(await screen.findByText(/could not be read/i)).toBeInTheDocument()
    expect(screen.queryByDisplayValue('90')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /set split/i })).not.toBeInTheDocument()
  })
})
