import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { setAuth, clearAuth } from '@/lib/auth'
import type { BudgetRow } from '@/lib/finops'
import { Budgets } from './Finops'

// Mock the shared API layer so we can assert the budget edit reuses PUT /projects/{name}.
const apiFetch = vi.fn(() =>
  Promise.resolve({ name: 'research', quotaUpdated: false, budgetUpdated: true }),
)
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [])),
  useMe: () => ({ data: undefined }),
}))

const ROW: BudgetRow = {
  project: 'research',
  period: 'monthly',
  gpuHoursBudget: 100,
  costBudget: 500,
  gpuHoursRatio: 0.4,
  costRatio: 0.2,
  overBudget: false,
}

function renderBudgets(rows: BudgetRow[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Budgets budgets={rows} />
    </QueryClientProvider>,
  )
}

describe('FinOps Budgets — in-context edit parity (BL-015)', () => {
  beforeEach(() => {
    apiFetch.mockClear()
    localStorage.clear()
  })

  it('hides the edit affordance from viewers', () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderBudgets([ROW])
    expect(screen.queryByRole('button', { name: /Edit budget for research/i })).not.toBeInTheDocument()
  })

  it('lets an admin edit a budget through the shared PUT /projects path', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderBudgets([ROW])
    fireEvent.click(screen.getByRole('button', { name: /Edit budget for research/i }))
    fireEvent.change(screen.getByLabelText('GPU-hour budget for research'), {
      target: { value: '250' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiFetch).toHaveBeenCalled())
    const [path, opts] = apiFetch.mock.calls[0] as unknown as [string, { method: string; body: string }]
    expect(path).toBe('/api/v1/projects/research')
    expect(opts.method).toBe('PUT')
    expect(JSON.parse(opts.body)).toMatchObject({ gpuHoursBudget: 250, costBudget: 500 })
  })

  it('shows an admin-oriented empty state (no CLI-only hint)', () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderBudgets([])
    expect(screen.getByText(/Platform → Projects/i)).toBeInTheDocument()
    expect(screen.queryByText(/exa finops budget set/i)).not.toBeInTheDocument()
  })

  afterEach(() => clearAuth())
})
