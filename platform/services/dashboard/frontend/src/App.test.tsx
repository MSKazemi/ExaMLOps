import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import App, { RedirectSplat } from './App'
import { ROUTE_REDIRECTS } from '@/lib/nav'
import { clearAuth, setAuth } from '@/lib/auth'

describe('App', () => {
  beforeEach(() => {
    localStorage.clear()
    setAuth({ token: 'test', role: 'viewer', expiresAt: new Date(Date.now() + 60_000).toISOString() })
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify([]))))
  })

  afterEach(() => {
    clearAuth()
    vi.unstubAllGlobals()
    window.history.replaceState({}, '', '/')
  })

  it('renders a useful fallback and title for an unknown URL', async () => {
    window.history.replaceState({}, '', '/does-not-exist')
    render(<App />)

    expect(screen.getByText('Page not found')).toBeInTheDocument()
    await waitFor(() => expect(document.title).toBe('Page not found · ExaMLOps'))
  })
})

/**
 * The clean-slate URL migration (ADR 0097 §3): old flat paths redirect to their new lifecycle-scoped
 * homes, preserving any sub-path. We mount the same redirect construction App uses.
 */
describe('URL migration redirects', () => {
  function renderAt(path: string) {
    return render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          {Object.entries(ROUTE_REDIRECTS).map(([from, to]) => (
            <Route key={from} path={`${from}/*`} element={<RedirectSplat to={to} />} />
          ))}
          <Route path="/build/models" element={<div>MODELS LIST</div>} />
          <Route path="/build/models/:name" element={<div>MODEL DETAIL</div>} />
          <Route path="/govern/approvals" element={<div>APPROVALS</div>} />
          <Route path="/serve/nextgen" element={<div>NEXTGEN</div>} />
        </Routes>
      </MemoryRouter>,
    )
  }

  it('redirects an old leaf path to its new home', () => {
    renderAt('/models')
    expect(screen.getByText('MODELS LIST')).toBeInTheDocument()
  })

  it('redirects an old detail path preserving the sub-path', () => {
    renderAt('/models/JPCP')
    expect(screen.getByText('MODEL DETAIL')).toBeInTheDocument()
  })

  it('redirects a renamed console (approvals → govern/approvals)', () => {
    renderAt('/approvals')
    expect(screen.getByText('APPROVALS')).toBeInTheDocument()
  })

  it('redirects the hyphenated normalization (/next-gen → serve/nextgen)', () => {
    renderAt('/next-gen')
    expect(screen.getByText('NEXTGEN')).toBeInTheDocument()
  })
})
