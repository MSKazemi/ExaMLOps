import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { I18nProvider } from '@/hooks/I18nProvider'
import { buildNocSlides } from '@/lib/noc'
import { NocWall } from './NocWall'

describe('buildNocSlides', () => {
  it('composes spend / alerts / carbon slides from live data', () => {
    const slides = buildNocSlides(
      { cost: { total_cost_usd: 42, total_gpu_hours: 10 }, carbon: { co2e_kg: 3 } },
      { count: 2, counts: { critical: 1, warn: 1 } },
    )
    expect(slides.map((s) => s.id)).toEqual(['spend', 'alerts', 'carbon'])
    expect(slides[0].value).toContain('42')
    expect(slides[1].value).toBe('2')
  })

  it('degrades to placeholders when data is missing (never blank)', () => {
    const slides = buildNocSlides(undefined, undefined)
    expect(slides.every((s) => s.value === '—' || s.value.length > 0)).toBe(true)
    expect(slides[0].value).toBe('—')
  })
})

describe('NocWall', () => {
  beforeEach(() => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )
  })

  it('renders a chrome-less full-screen kiosk with a slide', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <I18nProvider>
          <MemoryRouter>
            <NocWall />
          </MemoryRouter>
        </I18nProvider>
      </QueryClientProvider>,
    )
    expect(screen.getByTestId('noc-wall')).toBeInTheDocument()
    expect(screen.getByText('ExaMLOps · NOC')).toBeInTheDocument()
    // no site navigation in kiosk mode
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
    // an exit affordance exists (never traps the operator)
    expect(screen.getByRole('link', { name: 'Exit kiosk' })).toBeInTheDocument()
  })
})
