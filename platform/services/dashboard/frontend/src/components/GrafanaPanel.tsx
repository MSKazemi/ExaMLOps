import { useState } from 'react'
import { BarChart3 } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import {
  GRAFANA_PANELS,
  buildPanelUrl,
  grafanaBaseUrl,
  type GrafanaPanelName,
  type BuildPanelUrlOptions,
} from '@/lib/grafana'

export interface GrafanaPanelProps extends BuildPanelUrlOptions {
  /** Registry key of the panel to embed (see `GRAFANA_PANELS`). */
  name: GrafanaPanelName
  /** Base Grafana URL; defaults to `VITE_GRAFANA_URL`. Pass the backend's public URL to override. */
  baseUrl?: string | null
  /** iframe height in px. */
  height?: number
  title?: string
}

/**
 * GrafanaPanel — a themed, config-driven embed of one provisioned Grafana panel (F5 / ADR 0054).
 *
 * Builds a `d-solo` iframe URL from the typed registry (never hardcoded), shows a Skeleton while the
 * iframe loads, and falls back to an accessible EmptyState when Grafana isn't configured/reachable —
 * so a missing dashboard never leaves a broken frame (R1). Heavy time-series stay in Grafana (R6).
 */
export function GrafanaPanel({
  name,
  baseUrl,
  height = 220,
  title,
  theme,
  timeRange,
  vars,
}: GrafanaPanelProps) {
  const [loaded, setLoaded] = useState(false)
  const url = buildPanelUrl(grafanaBaseUrl(baseUrl), GRAFANA_PANELS[name], { theme, timeRange, vars })

  if (!url) {
    return (
      <EmptyState
        icon={BarChart3}
        title="Grafana not configured"
        description="Set VITE_GRAFANA_URL (or the backend public Grafana URL) to embed this panel."
      />
    )
  }

  return (
    <div className="relative w-full overflow-hidden rounded-xl" style={{ height }}>
      {!loaded && <Skeleton className="absolute inset-0 h-full w-full" />}
      <iframe
        src={url}
        title={title ?? `Grafana panel: ${name}`}
        className="h-full w-full border-0"
        loading="lazy"
        onLoad={() => setLoaded(true)}
      />
    </div>
  )
}
