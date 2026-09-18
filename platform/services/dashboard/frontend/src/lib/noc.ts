import { usd, carbonLabel } from './finops'
import { inboxHeadline } from './alerts'
import { formatHpcUnit } from './i18n'

// NOC-wall slide composition (F20 / ADR 0069, R2). Pure — kept out of the component file so the page
// exports a component only (react-refresh) and the composition is unit-tested.

export interface NocSlide {
  id: string
  title: string
  value: string
  sub: string
}

/**
 * Curated NOC-wall slides from live platform signals. Missing data degrades to "—" rather than a
 * blank/crash — a wall must never show an error page.
 *
 * `unavailable` names the sources the BFF could not reach (its `_partial` list). Without it, a
 * source that **failed** and a source that has **not reported yet** are the same words on the wall
 * — "Awaiting data" — and on a wall that difference is the whole point: one resolves itself, the
 * other is someone's job. A dash with no explanation in a dark room reads as "quiet".
 */
export function buildNocSlides(
  finops:
    | { cost?: { total_cost_usd: number; total_gpu_hours: number }; carbon?: { co2e_kg: number; uncertainty?: number } }
    | undefined,
  alerts: { count: number; counts: Record<string, number> } | undefined,
  locale: 'en' | 'it' = 'en',
  unavailable: readonly string[] = [],
): NocSlide[] {
  const cost = finops?.cost
  const carbon = finops?.carbon
  const down = new Set(unavailable)
  // "Awaiting data" is for a source that has simply not reported; a source the BFF named as
  // unreachable says so instead.
  const missing = (source: string) => (down.has(source) ? `${source} source unavailable` : 'Awaiting data')
  return [
    {
      id: 'spend',
      title: 'GPU spend',
      value: cost ? usd(cost.total_cost_usd) : '—',
      sub: cost ? `${formatHpcUnit(cost.total_gpu_hours, 'GPU-h', locale)} this period` : missing('cost'),
    },
    {
      id: 'alerts',
      title: 'Active alerts',
      value: alerts ? String(alerts.count) : '—',
      sub: alerts ? inboxHeadline(alerts.counts) : missing('inbox'),
    },
    {
      id: 'carbon',
      title: 'Operational carbon (est.)',
      value: carbon ? carbonLabel(carbon.co2e_kg, carbon.uncertainty ?? 0) : '—',
      // ADR 0112 R-ee: an operational sum on a wall display must not read as the whole footprint.
      sub: carbon
        ? 'Embodied carbon not measured — not a total'
        : `${missing('carbon')} · embodied carbon not measured`,
    },
  ]
}
