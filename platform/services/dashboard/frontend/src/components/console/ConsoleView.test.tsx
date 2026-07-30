import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Boxes } from 'lucide-react'
import type { Column } from '@/lib/datagrid'
import { ConsoleView, type RowAction } from './index'

interface Row {
  id: string
  name: string
  status: 'pending' | 'done'
}

const COLUMNS: Column<Row>[] = [
  { key: 'name', header: 'Name', accessor: (r) => r.name, sortable: true },
  { key: 'status', header: 'Status', accessor: (r) => r.status, facet: true },
]

const ROWS: Row[] = [
  { id: 'a', name: 'alpha', status: 'pending' },
  { id: 'b', name: 'beta', status: 'done' },
]

function renderConsole(props: Partial<Parameters<typeof ConsoleView<Row>>[0]> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ConsoleView<Row>
        title="Widgets"
        subtitle="All the widgets"
        icon={Boxes}
        columns={COLUMNS}
        rows={ROWS}
        getRowId={(r) => r.id}
        {...props}
      />
    </QueryClientProvider>,
  )
}

describe('ConsoleView', () => {
  it('renders the title, subtitle and rows through the columns', () => {
    renderConsole()
    expect(screen.getByRole('heading', { name: 'Widgets' })).toBeInTheDocument()
    expect(screen.getByText('All the widgets')).toBeInTheDocument()
    expect(screen.getByText('alpha')).toBeInTheDocument()
    expect(screen.getByText('beta')).toBeInTheDocument()
  })

  it('shows a loading skeleton before rows arrive', () => {
    renderConsole({ rows: undefined, isLoading: true })
    expect(screen.getByLabelText('Loading Widgets')).toBeInTheDocument()
  })

  it('shows the error banner with the custom message', () => {
    renderConsole({ error: new Error('boom'), errorMessage: 'Cannot reach service' })
    expect(screen.getByText('Cannot reach service')).toBeInTheDocument()
  })

  it('renders an empty state when there are no rows', () => {
    renderConsole({ rows: [], emptyTitle: 'No widgets' })
    expect(screen.getByText('No widgets')).toBeInTheDocument()
  })

  it('runs a plain row action on click', async () => {
    const run = vi.fn().mockResolvedValue(undefined)
    const rowActions: RowAction<Row>[] = [{ id: 'go', label: 'Go', run }]
    renderConsole({ rowActions })
    // one button per row (2 rows)
    const buttons = screen.getAllByRole('button', { name: 'Go' })
    expect(buttons).toHaveLength(2)
    fireEvent.click(buttons[0])
    await waitFor(() => expect(run).toHaveBeenCalledTimes(1))
    expect(run.mock.calls[0][0]).toEqual(ROWS[0])
  })

  it('only shows an action when its visible predicate passes', () => {
    const rowActions: RowAction<Row>[] = [
      { id: 'resolve', label: 'Resolve', visible: (r) => r.status === 'pending', run: vi.fn() },
    ]
    renderConsole({ rowActions })
    // Only the pending row (alpha) gets the action.
    expect(screen.getAllByRole('button', { name: 'Resolve' })).toHaveLength(1)
  })

  it('requires a non-empty reason before running a needsReason action', async () => {
    const run = vi.fn().mockResolvedValue(undefined)
    const rowActions: RowAction<Row>[] = [{ id: 'reject', label: 'Reject', needsReason: true, run }]
    renderConsole({ rows: [ROWS[0]], rowActions })
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    const confirm = screen.getByRole('button', { name: 'Confirm' })
    expect(confirm).toBeDisabled() // no reason yet
    fireEvent.change(screen.getByLabelText('Reject reason'), { target: { value: 'bad data' } })
    expect(confirm).toBeEnabled()
    fireEvent.click(confirm)
    await waitFor(() => expect(run).toHaveBeenCalledWith(ROWS[0], { reason: 'bad data' }))
  })

  it('disables a capability-gated action when the role lacks the capability', () => {
    const run = vi.fn()
    const rowActions: RowAction<Row>[] = [
      { id: 'secure', label: 'Secure', capability: 'secrets.write', run },
    ]
    renderConsole({ rows: [ROWS[0]], rowActions })
    // No /api/auth/me in tests ⇒ no capabilities ⇒ the action is shown-but-disabled, never run.
    expect(screen.getByRole('button', { name: 'Secure' })).toBeDisabled()
    expect(run).not.toHaveBeenCalled()
  })
})
