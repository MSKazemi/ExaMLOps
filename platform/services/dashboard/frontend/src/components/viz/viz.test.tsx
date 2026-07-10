import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { KpiTile, Distribution, Uncertainty } from './index'

describe('KpiTile (F4 R2)', () => {
  it('renders value, unit, delta and label', () => {
    render(<KpiTile label="Queue depth" value={12} delta={3} threshold={{ warn: 5, crit: 20 }} />)
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.getByText('Queue depth')).toBeInTheDocument()
    expect(screen.getByText('+3')).toBeInTheDocument()
  })
})

describe('Distribution (F4 R5 + R7 a11y)', () => {
  it('renders an aria-labelled figure with a data-table fallback', () => {
    render(<Distribution values={[1, 2, 2, 3, 3, 3, 4]} bins={4} title="Predictions" />)
    // aria description present (F18)
    expect(screen.getAllByLabelText('Value distribution histogram').length).toBeGreaterThan(0)
    // data-table fallback present (F4 R7)
    expect(screen.getByText('Data table')).toBeInTheDocument()
    expect(screen.getByText('Count')).toBeInTheDocument()
  })

  it('shows "No data" for an empty series', () => {
    render(<Distribution values={[]} />)
    expect(screen.getByText('No data')).toBeInTheDocument()
  })
})

describe('Uncertainty (F4 R5 uncertainty)', () => {
  it('renders each variant mean with its CI in the table fallback', () => {
    render(
      <Uncertainty
        title="A/B"
        variants={[
          { label: 'A', mean: 1.0, ci: [0.8, 1.2] },
          { label: 'B', mean: 1.5, ci: [1.3, 1.7] },
        ]}
      />,
    )
    expect(screen.getByText('95% CI')).toBeInTheDocument()
    expect(screen.getByText('[0.80, 1.20]')).toBeInTheDocument()
  })
})
