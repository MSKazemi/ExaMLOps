import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { OnboardingTour } from './OnboardingTour'

describe('OnboardingTour', () => {
  beforeEach(() => localStorage.clear())

  it('runs on first visit and advances through steps', () => {
    render(<OnboardingTour />)
    expect(screen.getByRole('dialog', { name: 'Getting started' })).toBeInTheDocument()
    expect(screen.getByText('Welcome to ExaMLOps')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(screen.getByText('Find anything fast')).toBeInTheDocument()
  })

  it('is dismissed once skipped (does not reappear on remount) (GWT-5)', () => {
    const { unmount } = render(<OnboardingTour />)
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    unmount()
    render(<OnboardingTour />)
    expect(screen.queryByRole('dialog', { name: 'Getting started' })).not.toBeInTheDocument()
  })
})
