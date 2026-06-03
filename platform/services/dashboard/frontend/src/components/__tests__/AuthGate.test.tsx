import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { AuthGate } from '../AuthGate'

const ORIG_FETCH = globalThis.fetch

beforeEach(() => {
  localStorage.clear()
  globalThis.fetch = ORIG_FETCH
})

describe('AuthGate', () => {
  it('shows password form when no auth blob in storage', () => {
    render(
      <AuthGate>
        <div>protected</div>
      </AuthGate>,
    )
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument()
    expect(screen.queryByText('protected')).toBeNull()
  })

  it('renders children once login succeeds', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        token: 'abc.def.ghi',
        role: 'viewer',
        expires_at: new Date(Date.now() + 3600_000).toISOString(),
      }),
    }) as unknown as typeof fetch

    render(
      <AuthGate>
        <div>protected</div>
      </AuthGate>,
    )
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: 'secret' },
    })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))
    await waitFor(() => expect(screen.getByText('protected')).toBeInTheDocument())
  })

  it('shows error on 401 and stays on the form', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: 'invalid password' }),
    }) as unknown as typeof fetch

    render(
      <AuthGate>
        <div>protected</div>
      </AuthGate>,
    )
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: 'wrong' },
    })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))
    await waitFor(() =>
      expect(screen.getByText(/invalid password/i)).toBeInTheDocument(),
    )
    expect(screen.queryByText('protected')).toBeNull()
  })
})
