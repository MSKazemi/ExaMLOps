import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { CommentThread } from './CommentThread'
import type { Comment } from '@/lib/collab'

const COMMENTS: Comment[] = [
  { id: 1, author: 'alice', body: 'looks good', mentions: [], created_at: '2026-07-10 10:00' },
  { id: 2, author: 'bob', body: 'ping @carol', mentions: ['carol'], created_at: '2026-07-10 11:00' },
]

function renderThread() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <CommentThread entityType="models" entityId="jpcp" />
    </QueryClientProvider>,
  )
}

describe('CommentThread', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('renders existing comments with author + mentions', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ comments: COMMENTS }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )
    renderThread()
    await waitFor(() => expect(screen.getByText('looks good')).toBeInTheDocument())
    expect(screen.getByText('@carol')).toBeInTheDocument()
  })

  it('previews who a draft will notify and posts the comment', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ comments: [] }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
      )
      .mockResolvedValue(
        new Response(JSON.stringify({ id: 3, author: 'me', body: 'hey @dan', mentions: ['dan'], created_at: 'now' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    renderThread()
    await waitFor(() => expect(screen.getByText(/No comments yet/)).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Add a comment'), { target: { value: 'hey @dan' } })
    expect(screen.getByText(/Will notify: @dan/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Post comment' }))
    await waitFor(() => {
      const posted = fetchMock.mock.calls.find((c) => (c[1] as RequestInit)?.method === 'POST')
      expect(posted).toBeTruthy()
    })
  })
})
