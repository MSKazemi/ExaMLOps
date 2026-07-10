import { useEffect, useRef, useState } from 'react'
import { getToken, clearAuth } from './auth'
import {
  readEventStream,
  nextConnectionState,
  type ConnectionState,
  type SSEEvent,
} from './realtime'

const RECONNECT_DELAY_MS = 3000

/**
 * Subscribe a component to the dashboard's realtime SSE gateway (F8 R5).
 *
 * Opens `GET /api/v1/stream` with the auth token (fetch-streaming, since `EventSource` can't set
 * headers), forwards each parsed event to `onEvent`, and returns the live connection state
 * (`live` / `reconnecting` / `polling`) for a {@link ConnectionBadge}. Reconnects with a fixed
 * delay; after repeated failure the state escalates to `polling` so the surface can fall back to
 * REST polling. Aborts cleanly on unmount.
 */
export function useRealtime(
  channels: string,
  onEvent: (event: SSEEvent) => void,
): ConnectionState {
  const [state, setState] = useState<ConnectionState>('reconnecting')
  // Keep the latest handler without re-subscribing the stream on every render.
  const handler = useRef(onEvent)
  useEffect(() => {
    handler.current = onEvent
  })

  useEffect(() => {
    const controller = new AbortController()
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    async function connect() {
      try {
        const token = getToken()
        const res = await fetch(`/api/v1/stream?channels=${encodeURIComponent(channels)}`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
          signal: controller.signal,
        })
        // An expired/invalid token must log the user out (like apiFetch), not loop
        // "reconnecting" forever against a stream that will keep 401ing.
        if (res.status === 401 || res.status === 403) {
          clearAuth()
          window.location.reload()
          return
        }
        if (!res.ok || !res.body) throw new Error(`stream ${res.status}`)
        if (!cancelled) setState('live')
        await readEventStream(res.body, (e) => handler.current(e))
        throw new Error('stream ended') // server closed → reconnect
      } catch {
        if (cancelled) return
        setState((s) => nextConnectionState(s, false))
        timer = setTimeout(connect, RECONNECT_DELAY_MS)
      }
    }

    connect()
    return () => {
      cancelled = true
      controller.abort()
      if (timer) clearTimeout(timer)
    }
  }, [channels])

  return state
}
