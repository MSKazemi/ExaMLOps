import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'

// Mock the shared fetch layer so we assert the URL/shape without touching the network.
vi.mock('./api', () => ({
  apiFetch: vi.fn(),
}))

import { apiFetch } from './api'
import { useChallengerStatus, type ChallengerStatus } from './challenger'

const mockFetch = vi.mocked(apiFetch)

const wrapper = ({ children }: { children: ReactNode }) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return createElement(QueryClientProvider, { client: qc }, children)
}

describe('useChallengerStatus', () => {
  beforeEach(() => mockFetch.mockReset())

  it('asks the per-model challenger endpoint', async () => {
    mockFetch.mockResolvedValue({ model: 'JPCP', configured: false } as ChallengerStatus)
    const { result } = renderHook(() => useChallengerStatus('JPCP'), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(mockFetch).toHaveBeenCalledWith('/api/challenger/JPCP')
  })

  it('does not fetch until a model is selected', () => {
    renderHook(() => useChallengerStatus(null), { wrapper })
    expect(mockFetch).not.toHaveBeenCalled()
  })

  it('treats an unconfigured model as data, not an error', async () => {
    // The console asks for whichever model is selected; "no challenger here" is an ordinary
    // answer and must not put the page into an error state.
    mockFetch.mockResolvedValue({ model: 'NOPE', configured: false } as ChallengerStatus)
    const { result } = renderHook(() => useChallengerStatus('NOPE'), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.configured).toBe(false)
    expect(result.current.isError).toBe(false)
  })

  it('carries the numbers the promotion decision rests on', async () => {
    mockFetch.mockResolvedValue({
      model: 'JPCP',
      configured: true,
      n: 120,
      delta: 0.031,
      p_value: 0.004,
      significant: true,
      slo_ok: true,
      slo_reason: 'no SLO regression',
      policy_met: true,
    } as ChallengerStatus)
    const { result } = renderHook(() => useChallengerStatus('JPCP'), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    const d = result.current.data!
    expect(d.delta).toBe(0.031)
    expect(d.p_value).toBe(0.004)
    expect(d.significant).toBe(true)
    expect(d.policy_met).toBe(true)
  })
})
