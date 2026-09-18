# Dashboard Frontend Engineering Baseline

The dashboard frontend follows a small set of engineering conventions so pages stay typed, resilient,
and fast. This guide documents the baseline the codebase currently enforces and the parts still on the
roadmap.

- **Feature:** F23 · **Design:** ADR 0052 (`design/adr/0052-dashboard-frontend-engineering-baseline.md`) ·
  **Spec:** `design/vision/specs/F23-frontend-engineering-baseline.md`
- **Code:** `platform/services/dashboard/frontend/src/lib/errors.ts`, `lib/flags.ts`,
  `components/ErrorBoundary.tsx`, `App.tsx`

## Typed API errors (R2)

`apiFetch` throws a typed `ApiError` on any non-OK response:

```ts
try {
  await apiFetch('/api/v1/mlops/registry')
} catch (e) {
  if (e instanceof ApiError && e.status === 403) { /* forbidden */ }
  if (e instanceof ApiError) console.log(e.problem.detail)  // parsed RFC 7807 / FastAPI detail
}
```

`parseProblem()` normalizes three body shapes into a `Problem` (`{type,title,status,detail,instance}`):
RFC 7807 problem+json, FastAPI `{detail: string}`, and FastAPI validation `{detail: [{msg}]}`. It never
throws — a non-JSON body falls back to a generic title.

## Retry policy

`shouldRetry(failureCount, error, max)` is the single retry decision used by the `QueryClient`: it
**never retries a 4xx `ApiError`** (it won't succeed) but retries transient/5xx/network errors up to
`max`. Prefer this over per-query `retry` overrides.

## Error boundaries (resilience)

The route outlet is wrapped in `<ErrorBoundary>`, which catches render/runtime errors and shows a
designed `EmptyState` fallback with a **Try again** button instead of a blank white screen. Wrap any
independently-failing subtree in its own boundary if you want finer isolation.

## A failed load must not look like an empty platform

*The UI half of [honest degradation](honest-degradation.md), which catalogues the same defect across the platform.*

`<ErrorBoundary>` catches a *render* error. A **failed query** does not throw — `throwOnError` is
deliberately not set — so the component renders with `data === undefined`, and what the operator sees
is whatever that path draws. Two rules follow, and both have been got wrong here:

**A zero is a claim; a dash is the absence of one.** `0 device pools` asserts that none are
configured. A page that prints it because the request failed has made that claim without earning it,
and from the operator's chair it is indistinguishable from an idle platform. Render `—` when you do
not know, and say why:

```tsx
const { data, isLoading, error } = useThing()
const kpi = (n: number | undefined) => (error ? '—' : (n ?? 0))
```

**Say which state it is.** The consoles use `EmptyState` for both "nothing here" and "could not
load", distinguished by the title — `"No prompts yet"` against `"Couldn't load prompts"` — and 35
pages already do this. The NOC wall additionally separates *"Awaiting data"* (has not reported) from
*"<source> unavailable"* (the BFF could not reach it), because a wall is read from a distance and
silence there reads as quiet.

**Check it by rendering, not by reading.** Measuring this class with `grep` gave three different
wrong answers in one sitting — 27 pages, then 3, then 1 — because pages express the same intent in
different words. Mounting the page with a rejecting `apiFetch` and asserting on what the user is
shown is the only measurement that settled it, and it is two lines in a test:

```tsx
apiFetch.mockImplementation(() => Promise.reject(new Error('upstream 503')))
expect(container.textContent).not.toMatch(/0Device pools/)
```

## Route code-splitting (R6)

Heavy / less-frequent routes are `lazy()`-imported and rendered inside a `<Suspense>` skeleton, so they
don't inflate the initial bundle:

```tsx
const ModelDetail = lazy(() => import('@/pages/ModelDetail').then((m) => ({ default: m.ModelDetail })))
```

Add new heavy pages the same way; keep the landing route (`Overview`) eager.

## Feature flags (R7)

New surfaces ship behind a flag so they can be enabled/disabled without a redeploy:

```tsx
{isEnabled('mlopsConsole') && <Route path="/mlops" element={<MlopsConsole />} />}
```

`resolveFlag(name, env, storage)` layers **localStorage** (`flag:<name>`) over a **build-time env**
(`VITE_FLAG_<NAME>`) over the **registry default** in `lib/flags.ts`. To flip a flag locally:
`localStorage.setItem('flag:mlopsConsole', 'false')`. The full staged-rollout / targeting engine is
F25 — this is the read seam every new page uses.

## Verifying

```bash
cd platform/services/dashboard/frontend
npx tsc --noEmit                         # 0 errors
npx vitest run src/lib/errors.test.ts src/lib/flags.test.ts src/components/ErrorBoundary.test.tsx
```

(Node/npm aren't required locally — the suite runs in Docker: `docker run --rm -v "$PWD":/app -w /app
node:22-alpine sh -c "npm ci && npx vitest run"`.)

## Deferred (tracked in the dashboard-nextgen plan)

- OpenAPI-generated TypeScript client from the FastAPI schema + a CI **contract gate** that fails on
  drift (R1/R3) — replaces the hand-written `apiFetch` typing.
- Playwright **E2E** journeys (login → model → promote → approve → drift) in CI (R4).
- Storybook **visual-regression** snapshots + axe-core **a11y** checks in CI; `make dashboard-check`
  running the full pyramid (R5).
- **Bundle-size budget** enforced in CI + web-vitals tracking (R6).
- Per-PR ephemeral **preview deploys** (R7).
