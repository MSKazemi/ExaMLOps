import { render } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { Skeleton } from './skeleton'

describe('Skeleton', () => {
  it('renders a pulsing placeholder and merges custom classes', () => {
    const { container } = render(<Skeleton className="h-4 w-24" />)
    const el = container.querySelector('[data-slot="skeleton"]')
    expect(el).toBeTruthy()
    expect(el).toHaveClass('animate-pulse', 'h-4', 'w-24')
  })
})
