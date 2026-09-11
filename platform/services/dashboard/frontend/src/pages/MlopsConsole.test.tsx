import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MlopsConsole } from './MlopsConsole'
import type { PromotionCheck } from '@/lib/mlops'

// ADR 0008 clause 4: the Promotion panel shows the eval gate as its latest persisted report found
// it. It used to show `pass: true` whenever a promotion *policy* existed.
const FAILED: PromotionCheck = {
  model: 'JPCP',
  mlflowName: 'jpcp',
  policy: {
    allow: true,
    reasons: ['eval gate failed: v18 vs Production: accuracy'],
    metric: 'rmse',
    operator: '<',
    threshold: 5,
    fromAlias: 'Staging',
    toAlias: 'Production',
  },
  eval: {
    state: 'failed',
    pass: false,
    reason: 'v18 vs Production: accuracy',
    suite: 'jpcp-suite',
    baselineAlias: 'Production',
    mode: 'block',
    metrics: [
      {
        name: 'accuracy',
        candidate: 0.62,
        baseline: 0.9,
        delta: -0.28,
        min: 0.8,
        max_drop: null,
        failed: true,
        reason: 'below floor 0.8',
      },
    ],
    lastReport: {
      id: 7,
      candidate: '18',
      baseline: 'Production',
      passed: false,
      mode: 'block',
      ts: '2026-09-11 10:00:00',
      aggregate: 'all',
      metrics: [],
      judge: null,
      judgeEligible: true,
      judgeFailures: [],
      calibrationId: null,
    },
  },
  approval: { required: true, state: 'pending' },
  allowed: false,
}

vi.mock('@/lib/api', () => ({
  apiFetch: (url: string) => {
    if (url.includes('/mlops/registry'))
      return Promise.resolve({
        registry: {
          rows: [
            {
              name: 'JPCP',
              mlflowName: 'jpcp',
              version: 18,
              stage: 'Staging',
              health: 'ok',
              freshness: null,
              governed: true,
            },
          ],
          count: 1,
        },
      })
    if (url.includes('/mlops/promotion/')) return Promise.resolve({ promotion: FAILED })
    return Promise.resolve({})
  },
}))

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MlopsConsole />
    </QueryClientProvider>,
  )
}

describe('MLOps console — eval gate on the Promotion panel (ADR 0008)', () => {
  it('shows a failed gate as failed, with the metric that failed', async () => {
    renderPage()
    fireEvent.click(await screen.findByText('JPCP'))

    const section = await screen.findByRole('region', { name: 'Eval gate' })
    expect(within(section).getByText('Eval gate failed (v18)')).toBeInTheDocument()
    const row = section.querySelector('[data-metric="accuracy"]') as HTMLElement
    expect(within(row).getByText(/fail — below floor 0.8/)).toBeInTheDocument()
    expect(await screen.findByText(/Blocked — eval gate failed/)).toBeInTheDocument()
  })
})
