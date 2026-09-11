import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '@/lib/theme'
import { I18nProvider } from '@/hooks/I18nProvider'
import { setAuth } from '@/lib/auth'
import { Layout } from './Layout'

// Site feature profile (ADR 0128): a module the site switched off leaves the navigation. The
// server decides (`GET /api/v1/modules` → `disabled_pages`); the sidebar only follows it.

function renderAt(path: string, disabledPages?: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (disabledPages) {
    qc.setQueryData(['modules'], { available: true, modules: [], disabled_pages: disabledPages })
  }
  return render(
    <I18nProvider>
      <ThemeProvider>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[path]}>
            <Layout>
              <div>content</div>
            </Layout>
          </MemoryRouter>
        </QueryClientProvider>
      </ThemeProvider>
    </I18nProvider>,
  )
}

describe('Layout — site feature profile (ADR 0128)', () => {
  beforeEach(() => {
    localStorage.clear()
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
  })

  it('shows every page when the profile has not answered (every module on by default)', () => {
    renderAt('/operate/drift')
    expect(screen.getByRole('link', { name: /FinOps/i })).toBeInTheDocument()
  })

  it("hides the pages of a module the site switched off, and only those", () => {
    renderAt('/operate/drift', ['/operate/finops', '/operate/facility'])
    expect(screen.queryByRole('link', { name: /FinOps/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Facility/i })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Drift/i })).toBeInTheDocument()
  })
})
