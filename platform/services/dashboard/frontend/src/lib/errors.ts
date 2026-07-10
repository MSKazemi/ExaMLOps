// Typed API errors + retry policy (F23 R2 / ADR 0052).
//
// The dashboard surfaces API failures as a typed `ApiError` carrying the RFC 7807 problem+json
// fields when the backend provides them, so callers can branch on `status` / `title` instead of
// string-matching a generic Error message. `shouldRetry` centralizes the retry policy (never retry
// client errors) so every query behaves consistently.

/** RFC 7807 problem detail (the subset the dashboard uses). */
export interface Problem {
  type?: string
  title?: string
  status?: number
  detail?: string
  instance?: string
}

/** A typed API error thrown by the client. `status` is the HTTP status; `problem` is the parsed body. */
export class ApiError extends Error {
  readonly status: number
  readonly problem: Problem

  constructor(status: number, problem: Problem) {
    super(problem.detail || problem.title || `API error ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.problem = problem
  }

  /** True for 4xx (client) errors — these are not worth retrying. */
  get isClientError(): boolean {
    return this.status >= 400 && this.status < 500
  }
}

/**
 * Parse an error response body into a {@link Problem}. Accepts RFC 7807 problem+json
 * (`{type,title,status,detail,instance}`), FastAPI's `{detail: ...}`, or anything else (falls back
 * to a generic title). Never throws.
 */
export function parseProblem(status: number, body: unknown): Problem {
  if (body && typeof body === 'object') {
    const b = body as Record<string, unknown>
    // FastAPI: {detail: "..."} or {detail: [{msg}]}
    const detail =
      typeof b.detail === 'string'
        ? b.detail
        : Array.isArray(b.detail)
          ? b.detail.map((d) => (d && typeof d === 'object' ? (d as { msg?: string }).msg : String(d))).join('; ')
          : undefined
    return {
      type: typeof b.type === 'string' ? b.type : undefined,
      title: typeof b.title === 'string' ? b.title : undefined,
      status: typeof b.status === 'number' ? b.status : status,
      detail: detail ?? (typeof b.title === 'string' ? b.title : undefined),
      instance: typeof b.instance === 'string' ? b.instance : undefined,
    }
  }
  return { status, detail: `API error ${status}` }
}

/**
 * TanStack Query retry policy (F23): retry transient failures up to `max`, but never retry a 4xx
 * (an `ApiError` client error) — those won't succeed on retry.
 */
export function shouldRetry(failureCount: number, error: unknown, max = 1): boolean {
  if (error instanceof ApiError && error.isClientError) return false
  return failureCount < max
}
