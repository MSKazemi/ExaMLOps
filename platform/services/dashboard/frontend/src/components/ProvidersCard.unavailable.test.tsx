import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ProvidersCard } from './ProvidersCard'

let failing = true

// "No authored providers yet" is a claim about *which Python computes this project's numbers* —
// FinOps cost and carbon, drift, promotion. An operator who reads it concludes the stock formulas
// are in force. A read that failed must not be allowed to make that claim: an authored provider
// may well be active and governing every figure on the page.
vi.mock('@/lib/api', () => ({
  apiFetch: (): Promise<unknown> =>
    failing
      ? Promise.reject(new Error('providers unavailable'))
      : Promise.resolve([
          { domain: 'cost', name: 'c1', path: '/x/cost/c1.py', ok: true, error: null, active: true },
        ]),
  useMe: () => ({ data: { capabilities: [], tenant: 'default' } }),
}))

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ProvidersCard project="default" admin={false} />
    </QueryClientProvider>,
  )
}

describe('ProvidersCard — a failed read is not a statement about the calculations', () => {
  it('does not claim no providers are authored when the read failed', async () => {
    failing = true
    renderCard()
    expect(await screen.findByText(/could not be read/i)).toBeInTheDocument()
    expect(screen.queryByText(/No authored providers yet/i)).not.toBeInTheDocument()
  })

  // Without this, hiding the card entirely would pass every test — the mutant that always took
  // the error branch survived until it was added.
  it('still lists the authored providers when the read succeeds', async () => {
    failing = false
    renderCard()
    expect(await screen.findByText('c1')).toBeInTheDocument()
    expect(screen.queryByText(/could not be read/i)).not.toBeInTheDocument()
  })
})
