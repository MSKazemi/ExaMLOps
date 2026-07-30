import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '@/lib/theme'
import { I18nProvider } from '@/hooks/I18nProvider'
import { setAuth, clearAuth } from '@/lib/auth'
import { Layout } from './Layout'

function renderLayout(initialPath = '/') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
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
})
