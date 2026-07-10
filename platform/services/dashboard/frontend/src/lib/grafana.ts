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

/** Named registry of embeddable panels. Add entries here, reference them by key in components. */
export const GRAFANA_PANELS = {
  'drift.trend': { uid: 'examlops_drift', panelId: 3 },
  'overview.online': { uid: 'examlops_overview', panelId: 2 },
  'serving.latency': { uid: 'examlops_serving', panelId: 4 },
  'model.inferences': { uid: 'examlops_serving', panelId: 6 },
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
