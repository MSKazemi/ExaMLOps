import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { setAuth, clearAuth } from '@/lib/auth'
import { QuickActions } from './QuickActions'

function renderQA() {
  return render(
    <MemoryRouter>
      <QuickActions />
    </MemoryRouter>,
  )
}

describe('QuickActions (Home command-center, BL-025)', () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => clearAuth())

  it('shows primary destinations, admin-only ones included for admins', () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderQA()
    // A non-admin destination + an admin-only one (Approvals).
    expect(screen.getByRole('link', { name: /Models/i })).toHaveAttribute('href', '/build/models')
    expect(screen.getByRole('link', { name: /SLOs/i })).toHaveAttribute('href', '/operate/slos')
    expect(screen.getByRole('link', { name: /Approvals/i })).toHaveAttribute('href', '/govern/approvals')
  })

  it('hides admin-only quick actions from viewers', () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderQA()
    expect(screen.getByRole('link', { name: /Models/i })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Approvals/i })).not.toBeInTheDocument()
  })
})
