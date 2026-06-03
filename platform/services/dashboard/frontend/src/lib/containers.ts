import { apiFetch } from "./api"

export interface ContainerInfo {
  name: string
  display_name: string
  status: "running" | "exited" | "stopped" | "restarting" | "unknown"
  health: "healthy" | "unhealthy" | "starting" | "none"
  uptime: string
  image: string
}

export interface ContainersResponse {
  containers: ContainerInfo[]
}

export interface RestartSelfResponse {
  status: "restarting"
  reconnect_after_ms: number
}

export async function listContainers(): Promise<ContainersResponse> {
  return apiFetch<ContainersResponse>("/api/containers")
}

export async function startContainer(name: string): Promise<ContainerInfo> {
  return apiFetch<ContainerInfo>(`/api/containers/${name}/start`, { method: "POST" })
}

export async function stopContainer(name: string): Promise<ContainerInfo> {
  return apiFetch<ContainerInfo>(`/api/containers/${name}/stop`, { method: "POST" })
}

export async function restartContainer(
  name: string,
): Promise<ContainerInfo | RestartSelfResponse> {
  return apiFetch(`/api/containers/${name}/restart`, { method: "POST" })
}

export async function getLogs(name: string, lines = 100): Promise<string> {
  const data = await apiFetch<{ logs: string }>(
    `/api/containers/${name}/logs?lines=${lines}`,
  )
  return data.logs
}

/**
 * Opens an SSE connection to stream live container logs.
 * Uses fetch+ReadableStream (not EventSource) so we can pass the auth header.
 * Returns a cleanup function — call it to stop the stream.
 */
export function streamLogs(
  name: string,
  token: string | null,
  onLine: (line: string) => void,
): () => void {
  const controller = new AbortController()

  fetch(`/api/containers/${name}/logs/stream`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    signal: controller.signal,
  })
    .then(async (res) => {
      if (!res.ok || !res.body) return
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parts = buffer.split("\n\n")
        buffer = parts.pop() ?? ""
        for (const part of parts) {
          if (part.startsWith("data: ")) {
            onLine(part.slice(6))
          }
        }
      }
    })
    .catch(() => {
      // AbortError is expected on cleanup — ignore silently
    })

  return () => controller.abort()
}
