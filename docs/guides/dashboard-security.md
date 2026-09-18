# Dashboard Security Hardening

The dashboard ships a frontend + BFF **security-hardening baseline**: strict security headers on
every response, a rate-limit guard on expensive queries, and defense-in-depth markdown sanitization.

- **Feature:** F16 · **Design:** ADR 0053 (`design/adr/0053-dashboard-frontend-security-hardening.md`) ·
  **Spec:** `design/vision/specs/F16-frontend-security-hardening.md`
- **Backend:** `platform/services/dashboard/backend/security.py`
- **Frontend:** `platform/services/dashboard/frontend/src/lib/sanitize.ts`

## Security headers (R1)

`SecurityHeadersMiddleware` attaches these to **every** response:

| Header | Value / purpose |
|---|---|
| `Content-Security-Policy` | `default-src 'self'`; scripts same-origin; `frame-src`/`frame-ancestors` scoped to `'self'` + the Grafana embed origin (F5); `object-src 'none'`. |
| `Strict-Transport-Security` | `max-age=63072000; includeSubDomains` (HSTS). |
| `X-Content-Type-Options` | `nosniff`. |
| `Referrer-Policy` | `strict-origin-when-cross-origin`. |
| `Permissions-Policy` | camera/microphone/geolocation denied. |
| `X-Frame-Options` | `SAMEORIGIN` (legacy backstop for the CSP `frame-ancestors`). |

The Grafana origin comes from `settings.public_grafana_url`, so the F5 `d-solo` panels load while no
other site can iframe the dashboard. `build_csp()` is unit-tested — it's the security-sensitive part.

> **CSP rollout:** the current policy is enforced directly. A report-only → enforce rollout (with
> script nonces) is the recommended next step (F16 R1) and is tracked in the plan.

## Rate limiting (R7)

Expensive, UI-driven queries are guarded by an in-process fixed-window `RateLimiter`. The global
search endpoint (`GET /api/v1/search`, which fans out across every source per keystroke) returns
**429 Too Many Requests** (with `Retry-After`) once a client exceeds the window.

```python
_search_limiter = RateLimiter(limit=30, window_seconds=10.0)
# applied as a FastAPI dependency: Depends(rate_limit(_search_limiter))
```

Login is limited too — `POST /api/auth/login` at 10 attempts per minute per client — which is what
keeps the shared password from being brute-forced.

### The limiter's own memory is bounded

Hits were evicted *within* a key and the key itself was kept forever, so the map grew by one entry
per distinct client address and never shrank: 200 000 addresses cost about 160 MB and were still
resident long after their windows had passed. Login is unauthenticated, so an attacker rotating
IPv6 source addresses could grow the dashboard process without ever logging in — **a rate limiter
that can be made to exhaust memory is an amplifier, not a control.**

There are now three sweep triggers, for three different reasons memory should be released: enough
calls have gone by (busy), the map is over `max_keys` (flood), and a whole window has elapsed since
the last sweep (**quiet** — a process that saw a burst and then went idle must not hold those keys
until a thousand more requests happen to arrive). `max_keys` defaults to 10 000, roughly a kilobyte
each.

When the cap is reached the **least recently seen** keys go first, and *a refusal counts as being
seen*. That detail is the security of it: a refused request records no hit, so ordering eviction by
the recorded hits would evict precisely the clients being blocked and hand each a fresh allowance.
A client that keeps hammering while rotating addresses to flush the map therefore keeps its own
bucket and stays blocked.

The honest limit of any bounded map: a client that goes quiet long enough to become the stalest key
can be evicted, and its allowance starts again. That is inherent — the alternative is the unbounded
map this replaced — and it costs an attacker a pause longer than 10 000 other clients' activity to
buy one window of requests.

A multi-replica deployment should swap the in-process window for a shared store (Redis); the
dependency seam stays identical. Note that until then, *N* replicas mean *N* × the limit.

## Markdown sanitization (R2)

The dashboard renders authored markdown (Docs, model READMEs) with `react-markdown`, which does **not**
render raw HTML unless `rehype-raw` is added — injected markup is already inert. As defense-in-depth,
`sanitizeMarkdown()` runs first and strips the common vectors:

- `<script>` / `<style>` blocks
- framing / `link` / `meta` / `base` tags
- inline `on*=` event handlers
- `javascript:` / `vbscript:` / `data:text/html` URIs

`safeUrl()` similarly guards any href/src built from user input. **Never** introduce unsanitized
`dangerouslySetInnerHTML` (none exists in the tree, F16 R2) — route any future raw-HTML need through a
vetted allowlist sanitizer.

## Verifying

```bash
# backend headers + rate limiter
cd platform/services/dashboard/backend && python -m pytest tests/test_security.py -q

# frontend sanitizer
cd platform/services/dashboard/frontend && npx vitest run src/lib/sanitize.test.ts
```

## Deferred (tracked in the dashboard-nextgen plan)

- Report-only → enforce CSP with script nonces (R1).
- CSRF tokens on mutations (R3) and F15 session/step-up integration.
- Role-gated, audited secret **reveal** (R4) — config secrets are already masked at rest
  ([secrets guide](../dashboard/secrets.md)).
- Server-side PII redaction in log/audit/trace views (R5).
- CI SCA/dependency vulnerability scanning + SRI-pinned external assets (R6).
