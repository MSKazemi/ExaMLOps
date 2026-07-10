import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within, act } from '@testing-library/react'
import { DataGrid } from './DataGrid'
import type { Column } from '@/lib/datagrid'

interface Row {
  id: string
  model: string
  env: string
  cost: number
}

const COLUMNS: Column<Row>[] = [
  { key: 'model', header: 'Model', accessor: (r) => r.model, sortable: true },
  { key: 'env', header: 'Env', accessor: (r) => r.env, facet: true },
  { key: 'cost', header: 'Cost', accessor: (r) => r.cost, sortable: true },
]

const ROWS: Row[] = [
  { id: '1', model: 'jpcp', env: 'prod', cost: 30 },
  { id: '2', model: 'awgn', env: 'staging', cost: 10 },
  { id: '3', model: 'zeta', env: 'staging', cost: 20 },
]

function grid(props: Partial<React.ComponentProps<typeof DataGrid<Row>>> = {}) {
  return render(<DataGrid columns={COLUMNS} rows={ROWS} getRowId={(r) => r.id} {...props} />)
}

describe('DataGrid', () => {
  it('renders all rows and the row count', () => {
    grid()
    expect(screen.getByText('3 rows')).toBeInTheDocument()
    expect(screen.getByText('jpcp')).toBeInTheDocument()
  })

  it('sorts a column ascending then descending on header clicks', () => {
    grid()
    const sortBtn = screen.getByRole('button', { name: 'Sort by Cost' })
    fireEvent.click(sortBtn) // asc
    let cells = screen.getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell').pop()?.textContent)
    expect(cells).toEqual(['10', '20', '30'])
    fireEvent.click(sortBtn) // desc
    cells = screen.getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell').pop()?.textContent)
    expect(cells).toEqual(['30', '20', '10'])
  })

  it('filters via a facet chip', () => {
    grid()
    fireEvent.click(screen.getByRole('button', { name: 'staging (2)' }))
    expect(screen.getByText('2 rows')).toBeInTheDocument()
    expect(screen.queryByText('jpcp')).not.toBeInTheDocument()
  })

  it('runs a bulk action only after inline confirm (R4)', async () => {
    const onRun = vi.fn()
    grid({ bulkActions: [{ label: 'Delete', onRun, destructive: true }] })
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select row 1' }))
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    // not fired yet — awaiting confirm
    expect(onRun).not.toHaveBeenCalled()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    })
    expect(onRun).toHaveBeenCalledTimes(1)
    expect(onRun.mock.calls[0][0].map((r: Row) => r.id)).toEqual(['1'])
  })

  it('exposes a CSV export control', () => {
    grid({ label: 'costs' })
    expect(screen.getByRole('button', { name: 'Export costs as CSV' })).toBeInTheDocument()
  })
})
