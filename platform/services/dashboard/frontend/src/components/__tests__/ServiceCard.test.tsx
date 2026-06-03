import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { ServiceCard } from '../ServiceCard'

describe('ServiceCard', () => {
  it('shows service name and Open button', () => {
    render(
      <ServiceCard
        name="MLflow"
        url="http://localhost:5000"
        status="ok"
      />
    )
    expect(screen.getByText('MLflow')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /open/i })).toBeInTheDocument()
  })

  it('shows green badge for ok status', () => {
    render(<ServiceCard name="MLflow" url="http://localhost:5000" status="ok" />)
    expect(screen.getByText(/online/i)).toBeInTheDocument()
  })

  it('shows red badge for down status', () => {
    render(<ServiceCard name="MLflow" url="http://localhost:5000" status="down" />)
    expect(screen.getByText(/offline/i)).toBeInTheDocument()
  })

  it('shows yellow badge for degraded status', () => {
    render(<ServiceCard name="MLflow" url="http://localhost:5000" status="degraded" />)
    expect(screen.getByText(/degraded/i)).toBeInTheDocument()
  })
})
