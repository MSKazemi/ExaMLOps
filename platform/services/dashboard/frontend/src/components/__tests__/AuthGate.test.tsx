import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { AuthGate } from '../AuthGate'
import { getAuth } from '@/lib/auth'

const ORIG_FETCH = globalThis.fetch

type Route = { ok: boolean; status: number; body: unknown }

/** A fetch that answers by URL, so the gate's session probe and provider list are both covered. */
function routes(table: Record<string, Route>) {
  return vi.fn(async (url: string) => {
    const hit = Object.entries(table).find(([prefix]) => String(url).startsWith(prefix))
    const r = hit ? hit[1] : { ok: false, status: 404, body: {} }
    return { ok: r.ok, status: r.status, json: async () => r.body } as Response
  }) as unknown as typeof fetch
}

const NO_SESSION = { ok: false, status: 401, body: { detail: 'missing bearer token' } }
const NO_PROVIDERS = { ok: true, status: 200, body: { providers: [], local_login: true } }

beforeEach(() => {
  localStorage.clear()
  globalThis.fetch = routes({ '/api/auth/me': NO_SESSION, '/api/auth/sso/providers': NO_PROVIDERS })
})

afterEach(() => {
  globalThis.fetch = ORIG_FETCH
  window.history.replaceState(null, '', '/')
})

describe('AuthGate', () => {
  it('shows password form when there is no session', async () => {
    render(
      <AuthGate>
        <div>protected</div>
      </AuthGate>,
    )
    expect(await screen.findByLabelText(/password/i)).toBeInTheDocument()
    expect(screen.queryByText('protected')).toBeNull()
  })

  it('renders children once login succeeds', async () => {
    globalThis.fetch = routes({
      '/api/auth/me': NO_SESSION,
      '/api/auth/sso/providers': NO_PROVIDERS,
      '/api/auth/login': {
        ok: true,
        status: 200,
        body: {
          token: 'abc.def.ghi',
          role: 'viewer',
          expires_at: new Date(Date.now() + 3600_000).toISOString(),
        },
      },
    })
    render(
      <AuthGate>
        <div>protected</div>
      </AuthGate>,
    )
    fireEvent.change(await screen.findByLabelText(/password/i), { target: { value: 'secret' } })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))
    await waitFor(() => expect(screen.getByText('protected')).toBeInTheDocument())
    expect(getAuth()?.via).toBe('password')
  })

  it('shows error on 401 and stays on the form', async () => {
    globalThis.fetch = routes({
      '/api/auth/me': NO_SESSION,
      '/api/auth/sso/providers': NO_PROVIDERS,
      '/api/auth/login': { ok: false, status: 401, body: { detail: 'invalid password' } },
    })
    render(
      <AuthGate>
        <div>protected</div>
      </AuthGate>,
    )
    fireEvent.change(await screen.findByLabelText(/password/i), { target: { value: 'wrong' } })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))
    await waitFor(() => expect(screen.getByText(/invalid password/i)).toBeInTheDocument())
    expect(screen.queryByText('protected')).toBeNull()
  })

  // ── organisation SSO (ADR 0120) ──

  it('offers each data center that has dashboard SSO', async () => {
    globalThis.fetch = routes({
      '/api/auth/me': NO_SESSION,
      '/api/auth/sso/providers': {
        ok: true,
        status: 200,
        body: {
          providers: [{ name: 'jsc', display_name: 'Jülich Supercomputing Centre', login_url: '/api/auth/sso/jsc/login' }],
          local_login: true,
        },
      },
    })
    render(
      <AuthGate>
        <div>protected</div>
      </AuthGate>,
    )
    const link = await screen.findByRole('link', { name: /sign in with jülich supercomputing centre/i })
    expect(link.getAttribute('href')).toMatch(/^\/api\/auth\/sso\/jsc\/login\?return_to=/)
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument() // break-glass still offered
  })

  it('hides the password form when local login is disabled', async () => {
    globalThis.fetch = routes({
      '/api/auth/me': NO_SESSION,
      '/api/auth/sso/providers': {
        ok: true,
        status: 200,
        body: {
          providers: [{ name: 'jsc', display_name: 'JSC', login_url: '/api/auth/sso/jsc/login' }],
          local_login: false,
        },
      },
    })
    render(
      <AuthGate>
        <div>protected</div>
      </AuthGate>,
    )
    await screen.findByRole('link', { name: /sign in with jsc/i })
    expect(screen.queryByLabelText(/password/i)).toBeNull()
  })

  it('recognises an existing SSO cookie session without showing the form', async () => {
    globalThis.fetch = routes({
      '/api/auth/me': {
        ok: true,
        status: 200,
        body: {
          role: 'operator',
          expires_at: new Date(Date.now() + 3600_000).toISOString(),
          auth_method: 'sso',
          idp: 'jsc',
        },
      },
    })
    render(
      <AuthGate>
        <div>protected</div>
      </AuthGate>,
    )
    await waitFor(() => expect(screen.getByText('protected')).toBeInTheDocument())
    const blob = getAuth()
    expect(blob).toMatchObject({ token: '', role: 'operator', via: 'sso', idp: 'jsc' })
  })

  it('explains why an SSO attempt came back without a session', async () => {
    window.history.replaceState(null, '', '/?sso_error=no_role')
    render(
      <AuthGate>
        <div>protected</div>
      </AuthGate>,
    )
    expect(await screen.findByText(/grants you no ExaMLOps role/i)).toBeInTheDocument()
    expect(window.location.search).toBe('')
  })
})
