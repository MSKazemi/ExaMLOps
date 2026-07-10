import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Config } from '../../pages/Config'
import { setAuth } from '@/lib/auth'

/** Sections are collapsible and default collapsed; expand one by its header before querying. */
async function expandSection(name: RegExp) {
  fireEvent.click(await screen.findByRole('button', { name }))
}

const renderConfig = () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Config />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  vi.restoreAllMocks()

  globalThis.fetch = vi.fn(async (url: RequestInfo) => {
    const u = String(url)
    if (u.endsWith('/api/auth/me')) {
      return new Response(
        JSON.stringify({ role: 'admin', expires_at: new Date(Date.now() + 3600_000).toISOString() }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      )
    }
    if (u.endsWith('/api/config')) {
      return new Response(
        JSON.stringify({ mlflow_url: 'http://m', grafana_api_key: '***', minio_access_key: null, minio_url: 'http://s3' }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      )
    }
    if (u.endsWith('/api/config/keys')) {
      return new Response(
        JSON.stringify([
          { key: 'mlflow_url', is_secret: false, has_value: true, updated_at: '2026-01-01T00:00:00Z' },
          { key: 'grafana_api_key', is_secret: true, has_value: true, updated_at: '2026-01-01T00:00:00Z' },
          { key: 'minio_access_key', is_secret: true, has_value: false, updated_at: '2026-01-01T00:00:00Z' },
          { key: 'minio_url', is_secret: false, has_value: true, updated_at: '2026-01-01T00:00:00Z' },
        ]),
        { status: 200, headers: { 'content-type': 'application/json' } },
      )
    }
    return new Response('not found', { status: 404 })
  }) as unknown as typeof fetch
})

describe('Config page', () => {
  it('shows Save button for admin', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderConfig()
    expect(await screen.findByRole('button', { name: /save/i })).toBeInTheDocument()
  })

  it('shows read-only banner and hides Save button for viewer', async () => {
    setAuth({ token: 't', role: 'viewer', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    globalThis.fetch = vi.fn(async (url: RequestInfo) => {
      const u = String(url)
      if (u.endsWith('/api/auth/me')) {
        return new Response(
          JSON.stringify({ role: 'viewer', expires_at: new Date(Date.now() + 3600_000).toISOString() }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        )
      }
      if (u.endsWith('/api/config')) {
        return new Response(
          JSON.stringify({ mlflow_url: 'http://m' }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        )
      }
      if (u.endsWith('/api/config/keys')) {
        return new Response(
          JSON.stringify([{ key: 'mlflow_url', is_secret: false, has_value: true, updated_at: '2026-01-01T00:00:00Z' }]),
          { status: 200, headers: { 'content-type': 'application/json' } },
        )
      }
      return new Response('not found', { status: 404 })
    }) as unknown as typeof fetch

    renderConfig()
    expect(await screen.findByText(/read-only/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /save/i })).toBeNull()
  })

  it('renders a masked password input for a stored secret (never exposes the value)', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderConfig()
    await expandSection(/credentials/i)
    const input = await screen.findByLabelText(/grafana api key/i) as HTMLInputElement
    expect(input.type).toBe('password')
    // A stored secret shows a "value is set" placeholder, not the secret itself.
    expect(input.placeholder).toMatch(/stored/i)
    expect(input.value).toBe('')
  })

  it('renders Not set placeholder for unset secrets', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderConfig()
    await expandSection(/credentials/i)
    const input = await screen.findByLabelText(/minio access key/i) as HTMLInputElement
    expect(input.placeholder).toBe('Not set')
  })

  it('includes a minio_url field in the Endpoints section', async () => {
    setAuth({ token: 't', role: 'admin', expiresAt: new Date(Date.now() + 3600_000).toISOString() })
    renderConfig()
    await expandSection(/service endpoints/i)
    expect(await screen.findByLabelText(/minio s3 api url/i)).toBeInTheDocument()
  })
})
