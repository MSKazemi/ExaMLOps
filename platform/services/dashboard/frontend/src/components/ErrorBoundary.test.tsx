import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ErrorBoundary } from './ErrorBoundary'

function Boom({ crash }: { crash: boolean }): React.ReactElement {
  if (crash) throw new Error('kaboom')
  return <div>safe content</div>
}

describe('ErrorBoundary (F23 resilience)', () => {
  beforeEach(() => vi.spyOn(console, 'error').mockImplementation(() => {}))
  afterEach(() => vi.restoreAllMocks())

  it('renders children when there is no error', () => {
    render(
      <ErrorBoundary>
        <Boom crash={false} />
      </ErrorBoundary>,
    )
    expect(screen.getByText('safe content')).toBeInTheDocument()
  })

  it('renders the designed fallback with the error message on crash', () => {
    render(
      <ErrorBoundary>
        <Boom crash={true} />
      </ErrorBoundary>,
    )
    expect(screen.getByText('Something went wrong on this page')).toBeInTheDocument()
    expect(screen.getByText('kaboom')).toBeInTheDocument()
    // fallback offers a retry affordance
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })

  it('renders a custom fallback when provided', () => {
    render(
      <ErrorBoundary fallback={<div>custom fallback</div>}>
        <Boom crash={true} />
      </ErrorBoundary>,
    )
    expect(screen.getByText('custom fallback')).toBeInTheDocument()
  })

  it('reset clears the error and re-renders children', () => {
    const { rerender } = render(
      <ErrorBoundary>
        <Boom crash={true} />
      </ErrorBoundary>,
    )
    // Update the child to a non-crashing one first (the boundary still shows the fallback)…
    rerender(
      <ErrorBoundary>
        <Boom crash={false} />
      </ErrorBoundary>,
    )
    expect(screen.getByText('Something went wrong on this page')).toBeInTheDocument()
    // …then reset clears the error and the now-safe children render.
    fireEvent.click(screen.getByRole('button', { name: /try again/i }))
    expect(screen.getByText('safe content')).toBeInTheDocument()
  })
})
