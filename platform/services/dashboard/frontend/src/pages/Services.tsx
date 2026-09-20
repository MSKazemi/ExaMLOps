import { useState, useEffect, useRef } from 'react'
import { useContainers, useContainerAction, useMe, useConfig } from '@/lib/api'
import { getToken } from '@/lib/auth'
import { ContainerCard } from '@/components/ContainerCard'
import { Skeleton } from '@/components/ui/skeleton'
import type { ContainerInfo, RestartSelfResponse } from '@/lib/containers'

// Containers that have external UIs — maps service name → config key for URL
const EXTERNAL_SERVICE_CONFIG: Record<string, string> = {
  mlflow: "mlflow_url",
  orchestrator: "prefect_url",
  "ray-serving": "ray_dashboard_url",
  grafana: "grafana_url",
  prometheus: "prometheus_url",
  minio: "minio_console_url",
  "control-plane": "control_plane_url",
  jupyterhub: "jupyterhub_url",
}

const DESCRIPTIONS: Record<string, string> = {
  mlflow: "Model registry & experiment tracking",
  orchestrator: "Pipeline orchestration (Prefect)",
  "ray-serving": "Multi-model inference · Ray Dashboard",
  grafana: "Metrics dashboards",
  prometheus: "Metrics scraping",
  minio: "S3-compatible object storage",
  "control-plane": "Retrain trigger API",
  jupyterhub: "Multi-user notebook server (separate profile — make jupyter-up)",
  postgres: "MLflow metadata database",
  loki: "Log aggregation",
  promtail: "Log shipper",
  "dataplane-bus-sim": "Dataplane bus bus + job generator",
  "dataplane-bus-bridge": "Dataplane bus integration bridge",
  dashboard: "ExaMLOps dashboard (this service)",
}

function rewriteHost(url: string, requestHost: string): string {
  if (!url) return url
  try {
    const u = new URL(url)
    if (u.hostname === "localhost" && requestHost !== "localhost") {
      u.hostname = requestHost
    }
    return u.toString()
  } catch {
    return url
  }
}

export function Services() {
  const { data: me } = useMe()
  const role = me?.role ?? "viewer"
  // Auth is stored as a JSON blob under `dashboard_auth` — read it via getToken()
  // (the old direct `localStorage.getItem("auth_token")` was always null, so live
  // container-log streaming went out unauthenticated and silently 401'd).
  const token = getToken()

  const { data: configData } = useConfig()
  const config = configData ?? {}
  const host = window.location.hostname

  const { data: containersData, isError } = useContainers()
  const containers = containersData?.containers ?? []

  const { mutate: triggerAction } = useContainerAction()
  const [reconnecting, setReconnecting] = useState(false)

  // Track the reconnect poll interval so it's always torn down on unmount — the
  // callback calls window.location.reload()/setState and must not run afterwards.
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  useEffect(() => () => {
    if (pollRef.current !== null) clearInterval(pollRef.current)
  }, [])

  function pollUntilBack() {
    if (pollRef.current !== null) clearInterval(pollRef.current)
    let attempts = 0
    pollRef.current = setInterval(async () => {
      attempts += 1
      if (attempts > 30) {
        if (pollRef.current !== null) clearInterval(pollRef.current)
        pollRef.current = null
        setReconnecting(false)
        return
      }
      try {
        const r = await fetch("/api/health")
        if (r.ok) {
          if (pollRef.current !== null) clearInterval(pollRef.current)
          pollRef.current = null
          window.location.reload()
        }
      } catch {
        // backend still restarting — keep polling until /api/health responds
      }
    }, 2000)
  }

  function handleAction(name: string, action: 'start' | 'stop' | 'restart') {
    triggerAction(
      { name, action },
      {
        onSuccess: (result) => {
          if (result && 'reconnect_after_ms' in result) {
            setReconnecting(true)
            setTimeout(() => pollUntilBack(), (result as RestartSelfResponse).reconnect_after_ms)
          }
        },
      },
    )
  }

  const externalContainers = containers.filter(
    (c) => c.name in EXTERNAL_SERVICE_CONFIG,
  )
  const systemContainers = containers.filter(
    (c) => !(c.name in EXTERNAL_SERVICE_CONFIG),
  )

  function urlFor(c: ContainerInfo): string | undefined {
    const key = EXTERNAL_SERVICE_CONFIG[c.name]
    if (!key) return undefined
    const raw = (config as Record<string, string>)[key] ?? ""
    return raw ? rewriteHost(raw, host) : undefined
  }

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div>
        <h1 className="text-2xl font-bold">Services</h1>
        <p className="text-muted-foreground text-sm mt-1">
          {isError
            ? "Docker unavailable — status badges and controls are offline."
            : `${containers.length} containers in stack. Admins can start, stop, and restart.`}
        </p>
      </div>

      {/* External services with UIs */}
      <h2 className="text-sm font-medium text-muted-foreground mb-2">External Services</h2>
      {externalContainers.length === 0 && !isError && (
        <div className="space-y-2" aria-label="Loading services">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full" />
          ))}
        </div>
      )}
      {externalContainers.map((c) => (
        <ContainerCard
          key={c.name}
          container={c}
          role={role}
          url={urlFor(c)}
          description={DESCRIPTIONS[c.name]}
          onAction={role === "admin" ? handleAction : undefined}
          reconnecting={reconnecting && c.name === "dashboard"}
          token={token}
        />
      ))}

      {/* System containers — collapsed section */}
      {systemContainers.length > 0 && (
        <details style={{ marginTop: 24 }}>
          <summary className="cursor-pointer text-sm text-muted-foreground mb-2 select-none list-none">
            System Containers ({systemContainers.length})
          </summary>
          <div style={{ marginTop: 8 }}>
            {systemContainers.map((c) => (
              <ContainerCard
                key={c.name}
                container={c}
                role={role}
                description={DESCRIPTIONS[c.name]}
                onAction={role === "admin" ? handleAction : undefined}
                reconnecting={reconnecting && c.name === "dashboard"}
                token={token}
              />
            ))}
          </div>
        </details>
      )}
    </div>
  )
}
