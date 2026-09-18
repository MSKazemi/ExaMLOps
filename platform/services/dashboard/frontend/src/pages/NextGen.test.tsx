import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/**
 * A count of zero is a claim: "nothing is configured". A page that shows `0` because it could not
 * reach the platform has made that claim without earning it — an operator reads "0 device pools"
 * and concludes none exist, when the truth is that nobody could ask.
 *
 * This page rendered six zeroed KPI tiles on a completely failed load. It now shows a dash per
 * tile and says why, which is the rule the NOC wall already followed.
 */
const summary = {
  federated_runs: 3,
  device_pools: 2,
  placements: 7,
  autoscale_configs: 1,
  distributed_runs: 4,
  feature_views: 5,
}

const apiFetch = vi.fn()
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...(args as [string])),
  useMe: () => ({ data: undefined }),
}))

import { NextGen } from './NextGen'

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <NextGen />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('Next-Gen console', () => {
  it('shows the live counts when the platform answers', async () => {
    apiFetch.mockImplementation((path: string) =>
      path.includes('summary') ? Promise.resolve(summary) : Promise.resolve([]),
    )
    const { container } = renderPage()
    await waitFor(() => expect(container.textContent).toContain('Device pools'))
    await waitFor(() => expect(container.textContent).toContain('2'))
    expect(container.textContent).not.toContain("Couldn't load")
  })

  it('shows dashes, not zeros, when the summary cannot be read', async () => {
    apiFetch.mockImplementation(() => Promise.reject(new Error('upstream 503')))
    const { container } = renderPage()

    await waitFor(() => expect(screen.getByText(/Couldn't load the Next-Gen summary/)).toBeInTheDocument())

    const text = container.textContent ?? ''
    expect(text).toContain('—')
    expect(text).toContain('unknown, not zero')
    // The specific regression: six tiles reading 0 on a failed load.
    expect(text).not.toMatch(/0Federated runs/)
    expect(text).not.toMatch(/0Device pools/)
  })
})
