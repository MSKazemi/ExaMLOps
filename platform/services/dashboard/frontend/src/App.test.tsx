import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { RedirectSplat } from './App'
import { ROUTE_REDIRECTS } from '@/lib/nav'

describe('App', () => {
  it('module loads without error', () => {
    expect(true).toBe(true)
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
