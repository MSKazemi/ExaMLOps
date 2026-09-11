import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { handleAuthFailure } from './api'
import { getAuth, setAuth, ssoLoginUrl } from './auth'

// ADR 0120: what the SPA does with a 401/403 depends on how the session was established.

const ORIG_LOCATION = window.location
let assign: ReturnType<typeof vi.fn>
let reload: ReturnType<typeof vi.fn>

function res(status: number, wwwAuthenticate?: string): Response {
  return new Response(null, {
    status,
    headers: wwwAuthenticate ? { 'WWW-Authenticate': wwwAuthenticate } : {},
  })
}

const LATER = () => new Date(Date.now() + 3600_000).toISOString()

beforeEach(() => {
  localStorage.clear()
  assign = vi.fn()
  reload = vi.fn()
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { ...ORIG_LOCATION, pathname: '/models', search: '?tab=1', assign, reload },
  })
})

afterEach(() => {
  Object.defineProperty(window, 'location', { configurable: true, value: ORIG_LOCATION })
})

describe('handleAuthFailure', () => {
  it('sends an SSO session through step-up on insufficient_user_authentication', () => {
    setAuth({ token: '', role: 'operator', expiresAt: LATER(), via: 'sso', idp: 'jsc' })
    const navigated = handleAuthFailure(
      res(401, 'Bearer error="insufficient_user_authentication", acr_values="https://refeds.org/profile/mfa"'),
    )
    expect(navigated).toBe(true)
    expect(assign).toHaveBeenCalledWith('/api/auth/sso/jsc/login?return_to=%2Fmodels%3Ftab%3D1&step_up=true')
    expect(reload).not.toHaveBeenCalled()
  })

  it('re-authenticates an expired SSO session at its IdP (no step-up)', () => {
    setAuth({ token: '', role: 'viewer', expiresAt: LATER(), via: 'sso', idp: 'jsc' })
    expect(handleAuthFailure(res(401))).toBe(true)
    expect(assign).toHaveBeenCalledWith(ssoLoginUrl('jsc'))
    expect(getAuth()).toBeNull()
  })

  it('treats a 403 on an SSO session as an answer, not a broken session (no logout loop)', () => {
    setAuth({ token: '', role: 'viewer', expiresAt: LATER(), via: 'sso', idp: 'jsc' })
    expect(handleAuthFailure(res(403))).toBe(false)
    expect(getAuth()).not.toBeNull()
    expect(assign).not.toHaveBeenCalled()
    expect(reload).not.toHaveBeenCalled()
  })

  it('keeps the original behaviour for password sessions', () => {
    setAuth({ token: 't', role: 'admin', expiresAt: LATER(), via: 'password' })
    expect(handleAuthFailure(res(401))).toBe(true)
    expect(reload).toHaveBeenCalled()
    expect(getAuth()).toBeNull()
  })
})
