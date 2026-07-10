/**
 * Realtime client substrate for the dashboard's SSE gateway (F8 / ADR 0058, R5).
 *
 * The browser can't set an Authorization header on native `EventSource`, so live surfaces
 * consume `GET /api/v1/stream` via fetch-streaming. This module holds the *pure, testable*
 * pieces — SSE frame parsing and the connection-state model — so the React hook that wires
 * them to `fetch` stays thin.
 */

export type ConnectionState = 'live' | 'reconnecting' | 'polling'

export interface SSEEvent {
  /** The typed channel, e.g. `job.started` / `approval.approved`. `message` if unspecified. */
  event: string
  /** Parsed JSON payload, or the raw string if it wasn't JSON. */
  data: unknown
}

export interface ParseResult {
  events: SSEEvent[]
  /** Bytes after the last complete frame — carry into the next chunk. */
  rest: string
}

/**
 * Parse a chunk of an SSE stream into complete frames plus a leftover remainder.
 *
 * Frames are separated by a blank line. `event:` sets the channel (default `message`); `data:`
 * lines are concatenated with newlines. Lines starting with `:` are comments (keep-alives) and
 * are ignored. An incomplete trailing frame is returned in `rest` to prepend to the next chunk.
 */
export function parseSSEChunk(buffer: string): ParseResult {
  const events: SSEEvent[] = []
  // Split on blank lines; the final element is an incomplete frame (or '') to carry over.
  const blocks = buffer.split('\n\n')
  const rest = blocks.pop() ?? ''

  for (const block of blocks) {
    let event = 'message'
    const dataLines: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith(':') || line.trim() === '') continue // comment / keep-alive
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
    }
    if (dataLines.length === 0) continue // e.g. a pure comment block
    const raw = dataLines.join('\n')
    let data: unknown = raw
    try {
      data = JSON.parse(raw)
    } catch {
      /* leave as raw string */
    }
    events.push({ event, data })
  }
  return { events, rest }
}

/**
 * Next connection state given the current state and whether the stream is up.
 *
 * A first failure degrades `live → reconnecting` (a transient blip); a second consecutive
 * failure escalates to `polling` (give up on the stream, fall back to REST). Success returns
 * to `live` from any state. This drives the freshness badge and the poll fallback (R5).
 */
export function nextConnectionState(
  current: ConnectionState,
  streamUp: boolean,
): ConnectionState {
  if (streamUp) return 'live'
  if (current === 'live') return 'reconnecting'
  return 'polling'
}

/**
 * Read an SSE response body to completion, invoking `onEvent` for each parsed frame.
 *
 * Decodes bytes incrementally and threads the leftover remainder between chunks via
 * {@link parseSSEChunk}, so frames split across network reads are reassembled. Resolves when the
 * server closes the stream; the caller decides whether to reconnect.
 */
export async function readEventStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: SSEEvent) => void,
): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const { events, rest } = parseSSEChunk(buffer)
    buffer = rest
    for (const e of events) onEvent(e)
  }
}
