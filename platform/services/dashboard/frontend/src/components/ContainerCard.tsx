import { useState } from "react"
import { LogPanel } from "./LogPanel"
import type { ContainerInfo } from "../lib/containers"

interface Props {
  container: ContainerInfo
  role: "viewer" | "admin"
  url?: string
  description?: string
  onAction?: (name: string, action: "start" | "stop" | "restart") => void
  reconnecting?: boolean
  token: string | null
}

const STATUS_COLOR: Record<string, string> = {
  running: "#22c55e",
  restarting: "#f59e0b",
  starting: "#f59e0b",
  exited: "#ef4444",
  stopped: "#ef4444",
  unknown: "#6b7280",
}

export function ContainerCard({
  container,
  role,
  url,
  description,
  onAction,
  reconnecting = false,
  token,
}: Props) {
  const [expanded, setExpanded] = useState(false)
  const color = STATUS_COLOR[container.status] ?? STATUS_COLOR.unknown
  const isRunning = container.status === "running"

  return (
    <div
      style={{
        border: "1px solid #2a2a2a",
        borderRadius: 8,
        overflow: "hidden",
        marginBottom: 8,
      }}
    >
      {/* Header — always visible */}
      <div
        onClick={() => setExpanded((v) => !v)}
        style={{
          display: "flex",
          alignItems: "center",
          padding: "12px 16px",
          cursor: "pointer",
          userSelect: "none",
          background: expanded ? "#141414" : undefined,
        }}
      >
        <span
          style={{ color, marginRight: 8, fontSize: 10 }}
          aria-label={`status: ${container.status}`}
        >
          ●
        </span>
        <span style={{ flex: 1 }}>
          <span style={{ fontWeight: 600 }}>{container.display_name}</span>
          {description && (
            <span style={{ marginLeft: 8, fontSize: 12, color: "#888" }}>
              {description}
            </span>
          )}
          <span style={{ marginLeft: 8, fontSize: 11, color: "#666" }}>
            {container.status}
          </span>
        </span>
        <span style={{ color: "#555", fontSize: 10 }}>{expanded ? "▲" : "▼"}</span>
      </div>

      {/* Expanded panel */}
      {expanded && (
        <div style={{ padding: "0 16px 16px", borderTop: "1px solid #1a1a1a" }}>
          {/* Metadata row */}
          <div
            style={{
              marginTop: 10,
              marginBottom: 12,
              fontSize: 12,
              color: "#888",
              display: "flex",
              gap: 16,
            }}
          >
            {container.uptime && <span>Uptime: {container.uptime}</span>}
            {container.health !== "none" && (
              <span>Health: {container.health}</span>
            )}
            <span style={{ color: "#555" }}>{container.image}</span>
          </div>

          {/* Controls — admin only */}
          {role === "admin" && onAction && (
            <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
              <button
                onClick={() => onAction(container.name, "start")}
                disabled={isRunning}
                style={{ padding: "4px 12px", fontSize: 12 }}
              >
                Start
              </button>
              <button
                onClick={() => onAction(container.name, "stop")}
                disabled={!isRunning}
                style={{ padding: "4px 12px", fontSize: 12 }}
              >
                Stop
              </button>
              <button
                onClick={() => onAction(container.name, "restart")}
                style={{ padding: "4px 12px", fontSize: 12 }}
              >
                Restart
              </button>
            </div>
          )}

          {/* Self-restart reconnecting banner */}
          {reconnecting && (
            <div
              style={{
                background: "#2a1f00",
                border: "1px solid #f59e0b",
                borderRadius: 4,
                padding: "6px 12px",
                marginBottom: 12,
                fontSize: 12,
                color: "#f59e0b",
              }}
            >
              Dashboard restarting… Reconnecting automatically.
            </div>
          )}

          {/* Log panel */}
          <LogPanel containerName={container.name} token={token} />

          {/* External link */}
          {url && (
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              style={{
                display: "inline-block",
                marginTop: 12,
                fontSize: 12,
                color: "#60a5fa",
              }}
            >
              Open Service →
            </a>
          )}
        </div>
      )}
    </div>
  )
}
