import { useState, useEffect } from 'react'
import { Zap, KeyRound, Building2 } from 'lucide-react'
import uniboLogo from '@/assets/unibo.png'
import seanergysLogo from '@/assets/seanergys.jpg'
import { getAuth, setAuth, ssoLoginUrl, type Role } from '@/lib/auth'

interface SsoProvider {
  name: string
  display_name: string
  login_url: string
}

// Why an SSO attempt came back without a session (routers/sso.py `_fail`). Plain words; the
// audit log has the detail.
const SSO_ERRORS: Record<string, string> = {
  no_role: 'You signed in, but your organisation grants you no ExaMLOps role. Ask your administrator.',
  state_mismatch: 'The sign-in could not be verified. Please try again.',
  session_expired: 'The sign-in took too long. Please try again.',
  login_rejected: 'Your organisation\'s sign-in response was rejected.',
  unknown_provider: 'That organisation is no longer configured here.',
}

function ssoErrorText(code: string): string {
  if (code in SSO_ERRORS) return SSO_ERRORS[code]
  if (code.startsWith('idp_')) return `Your organisation's sign-in returned: ${code.slice(4)}`
  return 'Sign-in failed.'
}

export function AuthGate({ children }: { children: React.ReactNode }) {
  const [auth, setAuthState] = useState(getAuth())
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(() => {
    const code = new URLSearchParams(window.location.search).get('sso_error')
    return code ? ssoErrorText(code) : null
  })
  const [submitting, setSubmitting] = useState(false)
  const [providers, setProviders] = useState<SsoProvider[]>([])
  const [localLogin, setLocalLogin] = useState(true)
  // With no stored blob we first ask the BFF whether an SSO cookie session exists, so a signed-in
  // user never sees the login form flash.
  const [checking, setChecking] = useState(!getAuth())

  useEffect(() => {
    if (auth) return
    let cancelled = false
    const params = new URLSearchParams(window.location.search)
    if (params.has('sso_error')) {
      // Shown via the initial state above; drop it from the address bar so a reload is clean.
      params.delete('sso_error')
      const qs = params.toString()
      window.history.replaceState(null, '', window.location.pathname + (qs ? `?${qs}` : ''))
    }
    ;(async () => {
      try {
        const me = await fetch('/api/auth/me')
        if (me.ok) {
          const body = (await me.json()) as {
            role: Role
            expires_at: string
            auth_method?: string
            idp?: string | null
          }
          if (body.auth_method === 'sso' && !cancelled) {
            const blob = { token: '', role: body.role, expiresAt: body.expires_at, via: 'sso' as const, idp: body.idp }
            setAuth(blob)
            setAuthState(blob)
            return
          }
        }
      } catch { /* no session */ }
      try {
        const res = await fetch('/api/auth/sso/providers')
        if (res.ok) {
          const body = (await res.json()) as { providers: SsoProvider[]; local_login: boolean }
          if (!cancelled) {
            setProviders(body.providers ?? [])
            setLocalLogin(body.local_login !== false)
          }
        }
      } catch { /* providers unavailable: password login only */ }
      if (!cancelled) setChecking(false)
    })()
    return () => {
      cancelled = true
    }
  }, [auth])

  // Proactively log the user out when the token expires, even on a pure-realtime
  // page that never fires an apiFetch. getAuth() drops the expired blob, so
  // re-reading it returns null and the gate falls back to the login screen.
  useEffect(() => {
    if (!auth) return
    const msLeft = new Date(auth.expiresAt).getTime() - Date.now()
    const t = setTimeout(() => setAuthState(getAuth()), Math.max(0, msLeft))
    return () => clearTimeout(t)
  }, [auth])

  if (auth) return <>{children}</>
  if (checking) return null

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!password) return
    setSubmitting(true)
    setError(null)
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      })
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string }
        setError(body.detail ?? 'login failed')
        setSubmitting(false)
        return
      }
      const data = await res.json() as {
        token: string
        role: Role
        expires_at: string
      }
      const blob = { token: data.token, role: data.role, expiresAt: data.expires_at, via: 'password' as const }
      setAuth(blob)
      setAuthState(blob)
    } catch {
      setError('network error')
      setSubmitting(false)
    }
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center"
      style={{
        background:
          'radial-gradient(ellipse 60% 50% at 50% 50%, oklch(0.64 0.20 265 / 8%) 0%, transparent 70%),' +
          'var(--background)',
      }}
    >
      <div className="w-full max-w-sm space-y-6 px-8 py-8 rounded-2xl"
        style={{
          background: 'var(--surface-0)',
          border: '1px solid oklch(0.64 0.20 265 / 20%)',
          boxShadow: '0 0 60px oklch(0.64 0.20 265 / 10%), 0 25px 50px -12px oklch(0 0 0 / 40%)',
        }}
      >
        <div className="text-center space-y-3">
          <div className="flex justify-center">
            <div
              className="w-12 h-12 rounded-xl flex items-center justify-center"
              style={{
                background: 'oklch(0.64 0.20 265 / 18%)',
                border: '1px solid oklch(0.64 0.20 265 / 35%)',
                boxShadow: '0 0 20px oklch(0.64 0.20 265 / 25%)',
              }}
            >
              <Zap className="w-6 h-6" style={{ color: 'var(--accent-text)' }} />
            </div>
          </div>
          <div>
            <h1 className="text-2xl font-bold gradient-text">ExaMLOps</h1>
            <p className="text-sm text-muted-foreground mt-1">Sign in to continue</p>
          </div>
          <div className="flex items-center justify-center gap-3 pt-1">
            <img src={uniboLogo} alt="University of Bologna" className="h-6 w-6 object-contain opacity-60" />
            <div className="h-4 w-px bg-border" />
            <img src={seanergysLogo} alt="SEANERGYS" className="h-5 object-contain max-w-[72px] opacity-60" />
          </div>
        </div>

        {providers.length > 0 && (
          <div className="space-y-2" aria-label="Sign in with your organisation">
            {providers.map((p) => (
              <a
                key={p.name}
                href={ssoLoginUrl(p.name, { returnTo: window.location.pathname })}
                className="w-full inline-flex items-center justify-center gap-2 rounded-lg py-2.5 text-sm font-semibold transition-all duration-150 glow-primary"
                style={{
                  background: 'oklch(0.64 0.20 265)',
                  color: 'oklch(0.99 0 0)',
                  border: '1px solid oklch(0.64 0.20 265 / 60%)',
                }}
              >
                <Building2 className="w-4 h-4" /> Sign in with {p.display_name}
              </a>
            ))}
            {localLogin && (
              <p className="text-center text-[11px] text-muted-foreground pt-1">or use a local password</p>
            )}
          </div>
        )}
        {!localLogin && error && (
          <p className="text-xs" style={{ color: 'var(--error-text)' }}>{error}</p>
        )}
        {localLogin && (
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="password" className="text-xs font-medium text-muted-foreground">
              Password
            </label>
            <div className="relative">
              <KeyRound className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground/50" />
              <input
                id="password"
                type="password"
                placeholder="Enter password…"
                value={password}
                onChange={e => setPassword(e.target.value)}
                disabled={submitting}
                className="w-full rounded-lg pl-9 pr-3 py-2.5 text-sm placeholder:text-muted-foreground/50 focus:outline-none transition-all"
                style={{
                  background: 'var(--input-bg)',
                  border: '1px solid var(--border-md)',
                  color: 'var(--foreground)',
                }}
              />
            </div>
          </div>
          {error && (
            <p className="text-xs" style={{ color: 'var(--error-text)' }}>{error}</p>
          )}
          <button
            type="submit"
            disabled={submitting}
            className="w-full rounded-lg py-2.5 text-sm font-semibold transition-all duration-150 glow-primary disabled:opacity-50"
            style={{
              background: 'oklch(0.64 0.20 265)',
              color: 'oklch(0.99 0 0)',
              border: '1px solid oklch(0.64 0.20 265 / 60%)',
            }}
          >
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
        )}
      </div>
    </div>
  )
}
