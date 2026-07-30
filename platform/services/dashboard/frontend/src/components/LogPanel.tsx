import { useState, useEffect, useRef } from "react"
import { getLogs, streamLogs } from "../lib/containers"

interface Props {
  containerName: string
  token: string | null
}

export function LogPanel({ containerName, token }: Props) {
  const [lines, setLines] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [live, setLive] = useState(false)
  const stopRef = useRef<(() => void) | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  async function fetchSnapshot() {
    setLoading(true)
    try {
      const text = await getLogs(containerName, 100)
      setLines(text.split("\n").filter(Boolean))
    } catch {
      setLines(["Error fetching logs."])
    } finally {
      setLoading(false)
    }
  }

  function startLive() {
    setLive(true)
    stopRef.current = streamLogs(containerName, token, (line) => {
      setLines((prev) => [...prev.slice(-500), line])
    })
  }

  function stopLive() {
    setLive(false)
    stopRef.current?.()
    stopRef.current = null
  }

  function refresh() {
    stopLive()
    fetchSnapshot()
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: fetchSnapshot is an async fetch to an external system (with teardown), the canonical effect use case, not a synchronous state derivation.
    fetchSnapshot()
    return () => stopRef.current?.()
  }, [containerName])

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [lines])

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: "#888" }}>Logs</span>
        <button
          onClick={live ? stopLive : startLive}
          style={{ fontSize: 10, padding: "2px 8px" }}
          aria-label={live ? "Stop Live" : "Go Live"}
        >
          {live ? "Stop Live" : "Go Live"}
        </button>
        <button
          onClick={refresh}
          style={{ fontSize: 10, padding: "2px 8px" }}
          aria-label="Refresh"
        >
          ↺ Refresh
        </button>
        {live && (
          <span style={{ fontSize: 10, color: "#22c55e" }}>● LIVE</span>
        )}
      </div>

      <div
        ref={scrollRef}
        style={{
          background: "#080808",
          fontFamily: "monospace",
          fontSize: 11,
          lineHeight: 1.5,
          padding: "8px 10px",
          maxHeight: 200,
          overflowY: "auto",
          borderRadius: 4,
          border: "1px solid #1a1a1a",
          color: "#ccc",
        }}
      >
        {loading ? (
          <span style={{ color: "#555" }}>Loading…</span>
        ) : lines.length === 0 ? (
          <span style={{ color: "#555" }}>No logs available.</span>
        ) : (
          lines.map((line, i) => (
            <div key={i} style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
              {line}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
