import { describe, it, expect } from 'vitest'
import { parseSSEChunk, nextConnectionState, readEventStream, type SSEEvent } from './realtime'

/** Build a ReadableStream that emits the given text chunks as UTF-8 bytes. */
function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c))
      controller.close()
    },
  })
}

describe('parseSSEChunk', () => {
  it('parses a complete frame with a JSON payload', () => {
    const { events, rest } = parseSSEChunk('event: job.started\ndata: {"id": 1}\n\n')
    expect(rest).toBe('')
    expect(events).toEqual([{ event: 'job.started', data: { id: 1 } }])
  })

  it('defaults the channel to "message" and keeps non-JSON data as a string', () => {
    const { events } = parseSSEChunk('data: hello\n\n')
    expect(events).toEqual([{ event: 'message', data: 'hello' }])
  })

  it('ignores comment / keep-alive lines', () => {
    const { events } = parseSSEChunk(': keep-alive\n\nevent: drift.critical\ndata: {"m":"JPCP"}\n\n')
    expect(events).toEqual([{ event: 'drift.critical', data: { m: 'JPCP' } }])
  })

  it('returns an incomplete trailing frame as rest to carry into the next chunk', () => {
    const { events, rest } = parseSSEChunk('event: a\ndata: {"n":1}\n\nevent: b\ndata: {"n":2')
    expect(events).toEqual([{ event: 'a', data: { n: 1 } }])
    expect(rest).toBe('event: b\ndata: {"n":2')
    // Feeding the rest + the remainder completes the second frame.
    const cont = parseSSEChunk(rest + '}\n\n')
    expect(cont.events).toEqual([{ event: 'b', data: { n: 2 } }])
  })
})

describe('readEventStream', () => {
  it('emits every frame across chunk boundaries', async () => {
    const got: SSEEvent[] = []
    // The second frame is split across two chunks to exercise remainder carry-over.
    await readEventStream(
      streamOf(['event: hello\ndata: {"channels":["job.*"]}\n\n', 'event: job.started\nda', 'ta: {"id":7}\n\n']),
      (e) => got.push(e),
    )
    expect(got).toEqual([
      { event: 'hello', data: { channels: ['job.*'] } },
      { event: 'job.started', data: { id: 7 } },
    ])
  })

  it('resolves when the stream closes with no frames', async () => {
    const got: SSEEvent[] = []
    await readEventStream(streamOf([': keep-alive\n\n']), (e) => got.push(e))
    expect(got).toEqual([])
  })
})

describe('nextConnectionState', () => {
  it('returns live whenever the stream is up', () => {
    expect(nextConnectionState('polling', true)).toBe('live')
    expect(nextConnectionState('live', true)).toBe('live')
  })

  it('degrades live → reconnecting on the first failure, then → polling', () => {
    const s1 = nextConnectionState('live', false)
    expect(s1).toBe('reconnecting')
    expect(nextConnectionState(s1, false)).toBe('polling')
    expect(nextConnectionState('polling', false)).toBe('polling')
  })
})
