import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '@/lib/theme'
import { I18nProvider } from '@/hooks/I18nProvider'
import { setAuth, clearAuth } from '@/lib/auth'
import { Layout } from './Layout'

function renderLayout(initialPath = '/', flags?: Record<string, boolean>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // Seed the server's flag decisions, as `GET /api/v1/flags` would deliver them (F25 R2).
  if (flags) qc.setQueryData(['flags', 'decisions'], { flags })
  return render(
    <I18nProvider>
      <ThemeProvider>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[initialPath]}>
            <Layout>
              <div>content</div>
            </Layout>
          </MemoryRouter>
        </QueryClientProvider>
      </ThemeProvider>
    </I18nProvider>,
  )
}

describe('Layout — grouped shell (BL-013a)', () => {
  beforeEach(() => {
    localStorage.clear()
    // Admin so the admin-only Govern group is visible.
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
  })

  it('renders the six lifecycle group headers plus Overview', () => {
    renderLayout('/')
    for (const label of ['Build', 'Serve', 'Operate', 'Govern', 'Platform']) {
      expect(screen.getByRole('button', { name: new RegExp(label, 'i') })).toBeInTheDocument()
    }
    expect(screen.getByRole('link', { name: /Overview/i })).toBeInTheDocument()
  })

  it('starts every group collapsed and toggles a group open/closed on click', () => {
    renderLayout('/')
    // Default (no saved preference, non-active group): collapsed → items hidden.
    expect(screen.queryByRole('link', { name: /Drift/i })).not.toBeInTheDocument()
    // Click the Operate group header → it expands and its items appear.
    fireEvent.click(screen.getByRole('button', { name: /Operate/i }))
    expect(screen.getByRole('link', { name: /Drift/i })).toBeInTheDocument()
    // Click again → it collapses back.
    fireEvent.click(screen.getByRole('button', { name: /Operate/i }))
    expect(screen.queryByRole('link', { name: /Drift/i })).not.toBeInTheDocument()
  })

  it('keeps the active group open even if the user collapsed it', () => {
    // On /operate/drift the Operate group owns the active route, so Drift stays visible regardless.
    renderLayout('/operate/drift')
    fireEvent.click(screen.getByRole('button', { name: /Operate/i }))
    expect(screen.getByRole('link', { name: /Drift/i })).toBeInTheDocument()
  })

  it('hides the admin-only Govern group from viewers', () => {
    clearAuth()
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderLayout('/')
    expect(screen.queryByRole('button', { name: /Govern/i })).not.toBeInTheDocument()
    // Build (non-admin) is still there.
    expect(screen.getByRole('button', { name: /Build/i })).toBeInTheDocument()
  })

  it('shows the running examlops version in the sidebar footer', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['health'], { status: 'ok', checked_at: '2026-01-01T00:00:00Z', services: {}, version: '0.62.0' })
    render(
      <I18nProvider>
        <ThemeProvider>
          <QueryClientProvider client={qc}>
            <MemoryRouter initialEntries={['/']}>
              <Layout>
                <div>content</div>
              </Layout>
            </MemoryRouter>
          </QueryClientProvider>
        </ThemeProvider>
      </I18nProvider>,
    )
    expect(screen.getByText('examlops v0.62.0')).toBeInTheDocument()
  })

  it('shows nothing in the version slot before the first health response lands', () => {
    renderLayout('/')
    expect(screen.queryByText(/^examlops v/)).not.toBeInTheDocument()
  })

  it('opens and closes the compact navigation drawer', () => {
    renderLayout('/')
    const primaryNav = screen.getByRole('navigation', { name: 'Primary' })
    const drawer = primaryNav.closest('aside')
    expect(drawer).toHaveClass('hidden')

    fireEvent.click(screen.getByRole('button', { name: 'Open navigation' }))
    expect(drawer).toHaveClass('flex')
    expect(drawer).toHaveAttribute('role', 'dialog')
    expect(drawer).toHaveAttribute('aria-modal', 'true')

    fireEvent.click(within(drawer!).getByRole('button', { name: 'Close navigation' }))
    expect(drawer).toHaveClass('hidden')
  })
})

describe('Layout — server feature flags reach the nav (F25 R2)', () => {
  beforeEach(() => {
    localStorage.clear()
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
  })

  it('shows the CLI Console and Resources while cliConsole is on', () => {
    renderLayout('/platform/flags', { cliConsole: true })
    expect(screen.getByRole('link', { name: /CLI Console/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /^Resources$/ })).toBeInTheDocument()
  })

  it('hides both when an admin switches cliConsole off server-side', () => {
    renderLayout('/platform/flags', { cliConsole: false })
    expect(screen.queryByRole('link', { name: /CLI Console/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /^Resources$/ })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /^Flags$/ })).toBeInTheDocument() // the way back
  })
})
