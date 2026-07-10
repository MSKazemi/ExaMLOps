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
 */
export function buildNocSlides(
  finops:
    | { cost?: { total_cost_usd: number; total_gpu_hours: number }; carbon?: { co2e_kg: number; uncertainty?: number } }
    | undefined,
  alerts: { count: number; counts: Record<string, number> } | undefined,
  locale: 'en' | 'it' = 'en',
): NocSlide[] {
  const cost = finops?.cost
  const carbon = finops?.carbon
  return [
    {
      id: 'spend',
      title: 'GPU spend',
      value: cost ? usd(cost.total_cost_usd) : '—',
      sub: cost ? `${formatHpcUnit(cost.total_gpu_hours, 'GPU-h', locale)} this period` : 'Awaiting data',
    },
    {
      id: 'alerts',
      title: 'Active alerts',
      value: alerts ? String(alerts.count) : '—',
      sub: alerts ? inboxHeadline(alerts.counts) : 'Awaiting data',
    },
    {
      id: 'carbon',
      title: 'Estimated carbon',
      value: carbon ? carbonLabel(carbon.co2e_kg, carbon.uncertainty ?? 0) : '—',
      sub: 'Green-AI accounting',
    },
  ]
}
