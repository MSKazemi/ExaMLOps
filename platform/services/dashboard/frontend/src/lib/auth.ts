const KEY = 'dashboard_auth'

// `operator` (ADR 0120) runs the model lifecycle without the platform's keys.
export type Role = 'viewer' | 'operator' | 'admin'

export interface AuthBlob {
  // Empty for an organisation-SSO session: that session lives in an HttpOnly cookie the browser
  // sends by itself, and no token is ever exposed to JavaScript (RFC 10017, BFF pattern).
  token: string
  role: Role
  expiresAt: string  // ISO8601
  via?: 'password' | 'sso'
  idp?: string | null  // the data center whose IdP signed the user in (SSO only)
}

/** Header every state-changing request carries: proof of same-origin for cookie sessions. */
export const CSRF_HEADERS: Record<string, string> = { 'X-ExaMLOps-CSRF': '1' }

export const getAuth = (): AuthBlob | null => {
  const raw = localStorage.getItem(KEY)
  if (!raw) return null
  try {
    const blob = JSON.parse(raw) as AuthBlob
    if (new Date(blob.expiresAt).getTime() <= Date.now()) {
      localStorage.removeItem(KEY)
      return null
    }
    return blob
  } catch {
    localStorage.removeItem(KEY)
    return null
  }
}

export const setAuth = (blob: AuthBlob): void => {
  localStorage.setItem(KEY, JSON.stringify(blob))
}

export const clearAuth = (): void => {
  localStorage.removeItem(KEY)
}

export const getToken = (): string | null => getAuth()?.token || null
export const getRole = (): Role | null => getAuth()?.role ?? null
export const isAdmin = (): boolean => getRole() === 'admin'

/** Where to send the browser to (re-)authenticate with the session's IdP. */
export function ssoLoginUrl(idp: string, opts: { stepUp?: boolean; returnTo?: string } = {}): string {
  const params = new URLSearchParams()
  params.set('return_to', opts.returnTo ?? window.location.pathname + window.location.search)
  if (opts.stepUp) params.set('step_up', 'true')
  return `/api/auth/sso/${encodeURIComponent(idp)}/login?${params.toString()}`
}

/**
 * Sign out. An SSO session is ended server-side (the cookie is HttpOnly, so only the BFF can clear
 * it); when the center's IdP advertises an end-session endpoint the browser goes there too, so the
 * IdP session ends as well (RP-initiated logout).
 */
export async function signOut(): Promise<void> {
  const blob = getAuth()
  clearAuth()
  if (blob?.via === 'sso') {
    try {
      const res = await fetch('/api/auth/sso/logout', { method: 'POST', headers: CSRF_HEADERS })
      const body = (await res.json().catch(() => ({}))) as { end_session_url?: string | null }
      if (body.end_session_url) {
        window.location.assign(body.end_session_url)
        return
      }
    } catch {
      /* local sign-out still happened */
    }
  }
  window.location.reload()
}

// Back-compat shim used by lib/api.ts during migration; remove once api.ts no
// longer imports clearToken (already updated in this task — kept only as a
// safety net if any other file still imports it).
export const clearToken = clearAuth
