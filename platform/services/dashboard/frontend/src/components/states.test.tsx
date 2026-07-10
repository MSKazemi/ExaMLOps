import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { NotFound } from './NotFound'
import { Forbidden } from './Forbidden'

const inRouter = (ui: React.ReactNode) => render(<MemoryRouter>{ui}</MemoryRouter>)

describe('NotFound', () => {
  it('shows a generic message and a link back to the overview', () => {
    inRouter(<NotFound />)
    expect(screen.getByText('Page not found')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /back to overview/i })).toHaveAttribute('href', '/')
  })

  it('shows an entity-specific message when given an entity', () => {
    inRouter(<NotFound entity="Model" homeHref="/models" />)
    expect(screen.getByText('Model not found')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /back to overview/i })).toHaveAttribute('href', '/models')
  })
})

describe('Forbidden', () => {
  it('always explains why access is denied (F15)', () => {
    inRouter(<Forbidden />)
    expect(screen.getByText('Access denied')).toBeInTheDocument()
    expect(screen.getByText(/don't have permission to view this resource/i)).toBeInTheDocument()
  })

  it('names the specific resource in the explanation', () => {
    inRouter(<Forbidden resource="this tenant's models" />)
    expect(screen.getByText(/don't have permission to view this tenant's models/i)).toBeInTheDocument()
  })

  it('honours a full reason override', () => {
    inRouter(<Forbidden reason="Requires the sysadmin role." />)
    expect(screen.getByText('Requires the sysadmin role.')).toBeInTheDocument()
  })
})
