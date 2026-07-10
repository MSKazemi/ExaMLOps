import { renderHook, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { useRealtime } from './useRealtime'
import type { SSEEvent } from './realtime'

/** A stream that emits `chunks` and then stays open (like a real live SSE connection). */
function openStreamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c))
      // Intentionally not closed: a live SSE stream stays open, so state stays `live`.
    },
  })
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('useRealtime', () => {
  it('connects, reports live, and forwards parsed events', async () => {
    const events: SSEEvent[] = []
    const body = openStreamOf(['event: job.started\ndata: {"id":1}\n\n'])
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body, { status: 200 })))

    const { result, unmount } = renderHook(() => useRealtime('job.*', (e) => events.push(e)))
    await waitFor(() => expect(events.length).toBe(1))
    expect(events[0].data).toEqual({ id: 1 })
    expect(result.current).toBe('live')
    unmount()
  })

  it('falls back to polling when the stream cannot be opened', async () => {
    // Initial state is `reconnecting` (connecting…); a failed first attempt escalates to `polling`.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 503 })))
    const { result, unmount } = renderHook(() => useRealtime('job.*', () => {}))
    await waitFor(() => expect(result.current).toBe('polling'))
    unmount()
  })
})
