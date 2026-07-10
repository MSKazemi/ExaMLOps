/**
 * Status & severity semantics — the single, colourblind-safe source of truth for how the dashboard
 * conveys health and severity (ADR 0051 / feature F3, enforced by F18 accessibility).
 *
 * Every status maps to a distinct { label, icon, colorVar } so that colour is *never* the only cue:
 * a StatusPill always pairs the theme-aware colour with an icon and a text label (WCAG 2.2 AA).
 * `colorVar` names a CSS custom property defined per theme in `index.css` (night/day/midnight),
 * so pills stay legible and on-brand in every theme without relying on the `dark:` variant.
 */
import {
  CheckCircle2,
  TriangleAlert,
  XCircle,
  Clock,
  HelpCircle,
  Info,
  AlertCircle,
  type LucideIcon,
} from 'lucide-react'

export type HealthStatus = 'healthy' | 'degraded' | 'failed' | 'pending' | 'unknown'
export type Severity = 'info' | 'warn' | 'error' | 'critical'

export interface StatusMeta {
  /** Human-readable label — always rendered, so status is never conveyed by colour alone. */
  label: string
  /** Distinct icon per status — the non-colour cue required for colourblind safety (F18). */
  icon: LucideIcon
  /** Theme-aware CSS custom property (from index.css) used for the text + a tinted background. */
  colorVar: string
}

const HEALTH: Record<HealthStatus, StatusMeta> = {
  healthy: { label: 'Healthy', icon: CheckCircle2, colorVar: '--success-text' },
  degraded: { label: 'Degraded', icon: TriangleAlert, colorVar: '--warning-text' },
  failed: { label: 'Failed', icon: XCircle, colorVar: '--error-text' },
  pending: { label: 'Pending', icon: Clock, colorVar: '--accent-text' },
  unknown: { label: 'Unknown', icon: HelpCircle, colorVar: '--subtle-text' },
}

const SEVERITY: Record<Severity, StatusMeta> = {
  info: { label: 'Info', icon: Info, colorVar: '--accent-text' },
  warn: { label: 'Warning', icon: TriangleAlert, colorVar: '--warning-text' },
  error: { label: 'Error', icon: XCircle, colorVar: '--error-text' },
  critical: { label: 'Critical', icon: AlertCircle, colorVar: '--error-text' },
}

export const HEALTH_STATUSES = Object.keys(HEALTH) as HealthStatus[]
export const SEVERITIES = Object.keys(SEVERITY) as Severity[]

export function isHealthStatus(s: string): s is HealthStatus {
  return Object.prototype.hasOwnProperty.call(HEALTH, s)
}

export function isSeverity(s: string): s is Severity {
  return Object.prototype.hasOwnProperty.call(SEVERITY, s)
}

/**
 * Resolve display metadata for a health status or severity. Unrecognised strings fall back to the
 * neutral `unknown` health metadata so a pill always renders something legible.
 */
export function statusMeta(s: HealthStatus | Severity | string): StatusMeta {
  if (isHealthStatus(s)) return HEALTH[s]
  if (isSeverity(s)) return SEVERITY[s]
  return HEALTH.unknown
}

const HEALTHY_WORDS = new Set([
  'healthy', 'running', 'up', 'ok', 'online', 'ready', 'current', 'active', 'succeeded', 'success', 'passed',
])
const DEGRADED_WORDS = new Set(['degraded', 'warning', 'warn', 'stale', 'partial', 'slow', 'updated'])
const FAILED_WORDS = new Set(['failed', 'error', 'down', 'offline', 'crashed', 'unhealthy', 'fail', 'errored'])
const PENDING_WORDS = new Set([
  'pending', 'starting', 'provisioning', 'queued', 'deploying', 'loading', 'in_progress', 'running_slow',
])

/** Map an arbitrary backend status string onto a canonical HealthStatus. */
export function normalizeHealth(raw: string | null | undefined): HealthStatus {
  if (!raw) return 'unknown'
  const s = raw.trim().toLowerCase()
  if (HEALTHY_WORDS.has(s)) return 'healthy'
  if (DEGRADED_WORDS.has(s)) return 'degraded'
  if (FAILED_WORDS.has(s)) return 'failed'
  if (PENDING_WORDS.has(s)) return 'pending'
  return 'unknown'
}
