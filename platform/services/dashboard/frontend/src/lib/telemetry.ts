import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './api'

// Self-observability client seam (F24 / ADR 0067).
//
// No third-party telemetry egress (R3): UI actions are audited to the backend, and any error/vital
// payload is PII-scrubbed here before it would ever leave the browser. `scrubPii` is pure and
// unit-tested — it's the privacy-sensitive part.

// ── PII scrubbing (F16 / F24 R1) ──────────────────────────────────────────────

const EMAIL = /[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/gi
const BEARER = /Bearer\s+[A-Za-z0-9._-]+/gi
const JWT = /eyJ[A-Za-z0-9._-]{10,}/g
const LONG_HEX = /\b[0-9a-f]{24,}\b/gi

/** Remove common PII / secret tokens from a string before it is reported (F24 R1, no false send). */
export function scrubPii(input: string): string {
  if (!input) return ''
  return input
    .replace(EMAIL, '[email]')
    .replace(BEARER, 'Bearer [redacted]')
    .replace(JWT, '[token]')
    .replace(LONG_HEX, '[hex]')
}

// ── view types ────────────────────────────────────────────────────────────────

export interface DependencyHealth {
  name: string
  status: 'up' | 'degraded' | 'down'
  latencyMs?: number
  error?: string
}

export interface SelfObsStatus {
  status: 'up' | 'degraded'
  dependencies: DependencyHealth[]
  metrics: {
    requests: number
    errors: number
    clientErrors: number
    rateLimitHits: number
    latencyMs: { count: number; p50: number | null; p95: number | null }
  }
}

// ── reporting (no third-party egress) ──────────────────────────────────────────

/** Audit a UI action to the backend (`platform_db`, D4). Best-effort; never throws. */
export async function reportAction(action: string, target = '', details = ''): Promise<void> {
  try {
    await apiFetch('/api/v1/selfobs/action', {
      method: 'POST',
      body: JSON.stringify({ action, target, details: scrubPii(details) }),
    })
  } catch {
    /* observability must never break the UX */
  }
}

// ── status hook (in-app status page, R5) ────────────────────────────────────────

export const useSelfObsStatus = () =>
  useQuery<SelfObsStatus>({
    queryKey: ['selfobs', 'status'],
    queryFn: () => apiFetch<SelfObsStatus>('/api/v1/selfobs/status'),
    refetchInterval: 15_000,
  })
