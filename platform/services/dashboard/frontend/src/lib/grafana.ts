/**
 * Grafana embed layer config + URL builder (F5 / ADR 0054).
 *
 * Panel identifiers live in a typed registry (never hardcoded in components, R2); the base URL comes
 * from config (`VITE_GRAFANA_URL`, falling back to the backend's public Grafana URL). `buildPanelUrl`
 * is a pure function so the URL construction — the security-sensitive part — is unit-tested.
 */

export interface GrafanaPanelRef {
  /** Dashboard UID as provisioned in Grafana. */
  uid: string
  /** Numeric panel id within that dashboard. */
  panelId: number
}

/**
 * Named registry of embeddable panels. Add entries here, reference them by key in components.
 *
 * UIDs and panel ids MUST match the dashboards provisioned under
 * `platform/infra/docker-compose/grafana/provisioning/dashboards/*.json`. The provisioned UIDs
 * are hyphenated (`examlops-overview`, not `examlops_overview`), and serving panels live in the
 * `examlops-online-metrics` dashboard (there is no `examlops-serving`). Each panelId points at a
 * real time-series in that dashboard so the embed renders instead of "Dashboard/Panel not found".
 */
export const GRAFANA_PANELS = {
  // examlops-drift · panel 11 = "Prediction Value Median (per model)" (drift trend time-series)
  'drift.trend': { uid: 'examlops-drift', panelId: 11 },
  // examlops-overview · panel 31 = "Inference Rate by Model (req/s)" (platform activity trend)
  'overview.online': { uid: 'examlops-overview', panelId: 31 },
  // examlops-online-metrics · panel 20 = "Latency Percentiles — All Models Combined"
  'serving.latency': { uid: 'examlops-online-metrics', panelId: 20 },
  // examlops-online-metrics · panel 10 = "Request Rate by Model (success vs error)"
  'model.inferences': { uid: 'examlops-online-metrics', panelId: 10 },
} as const satisfies Record<string, GrafanaPanelRef>

export type GrafanaPanelName = keyof typeof GRAFANA_PANELS

export interface BuildPanelUrlOptions {
  theme?: 'light' | 'dark'
  /** Grafana time range, e.g. `{ from: 'now-6h', to: 'now' }`. */
  timeRange?: { from: string; to: string }
  /** Template variables → `var-<key>=<value>` (e.g. `{ model: 'JPCP' }`). */
  vars?: Record<string, string>
}

/**
 * Build a single-panel (`d-solo`) embed URL for a registered panel.
 *
 * Returns `null` when no base URL is configured, so the component can render an offline fallback
 * instead of a broken iframe (R1). Trailing slashes on the base are normalised.
 */
export function buildPanelUrl(
  baseUrl: string | null | undefined,
  panel: GrafanaPanelRef,
  opts: BuildPanelUrlOptions = {},
): string | null {
  if (!baseUrl) return null
  const base = baseUrl.replace(/\/+$/, '')
  const params = new URLSearchParams()
  params.set('orgId', '1')
  params.set('panelId', String(panel.panelId))
  params.set('kiosk', '')
  params.set('theme', opts.theme ?? 'dark')
  if (opts.timeRange) {
    params.set('from', opts.timeRange.from)
    params.set('to', opts.timeRange.to)
  }
  for (const [k, v] of Object.entries(opts.vars ?? {})) {
    params.set(`var-${k}`, v)
  }
  return `${base}/d-solo/${panel.uid}?${params.toString()}`
}

/** Resolve the configured Grafana base URL (build-time env → runtime fallback). */
export function grafanaBaseUrl(fallback?: string | null): string | null {
  const env = (import.meta as { env?: Record<string, string | undefined> }).env
  return env?.VITE_GRAFANA_URL ?? fallback ?? null
}
