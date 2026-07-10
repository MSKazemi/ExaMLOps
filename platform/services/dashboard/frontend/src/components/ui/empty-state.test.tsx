import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { Inbox } from 'lucide-react'
import { EmptyState } from './empty-state'

describe('EmptyState', () => {
  it('renders title, description and a call-to-action', () => {
    render(
      <EmptyState
        icon={Inbox}
        title="No models yet"
        description="Scaffold your first model to get started."
        action={<button>Scaffold</button>}
      />,
    )
    expect(screen.getByText('No models yet')).toBeInTheDocument()
    expect(screen.getByText('Scaffold your first model to get started.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Scaffold' })).toBeInTheDocument()
  })

  it('exposes an accessible region labelled by its title', () => {
    render(<EmptyState title="Nothing here" />)
    expect(screen.getByRole('region', { name: 'Nothing here' })).toBeInTheDocument()
  })

  it('renders a default icon when none is provided', () => {
    const { container } = render(<EmptyState title="Empty" />)
    expect(container.querySelector('svg')).toBeTruthy()
  })
})
