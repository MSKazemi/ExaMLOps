import { lazy, Suspense, useEffect } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useLocation, useParams } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '@/lib/theme'
import { I18nProvider } from '@/hooks/I18nProvider'
import { AnnouncerProvider } from '@/hooks/useAnnouncer'
import { useAnnouncer } from '@/hooks/announcer'
import { AuthGate } from '@/components/AuthGate'
import { Layout } from '@/components/Layout'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { Skeleton } from '@/components/ui/skeleton'
import { shouldRetry } from '@/lib/errors'
import { isEnabled } from '@/lib/flags'
import { ROUTE_REDIRECTS } from '@/lib/nav'
import { usePrefs } from '@/lib/prefs'
import { Overview } from '@/pages/Overview'
import { Services } from '@/pages/Services'
import { Models } from '@/pages/Models'

// Heavy / less-frequent routes are code-split (F23 R6) so they don't inflate the initial bundle.
const ModelDetail = lazy(() => import('@/pages/ModelDetail').then((m) => ({ default: m.ModelDetail })))
const Datasets = lazy(() => import('@/pages/Datasets').then((m) => ({ default: m.Datasets })))
const Config = lazy(() => import('@/pages/Config').then((m) => ({ default: m.Config })))
const Audit = lazy(() => import('@/pages/Audit').then((m) => ({ default: m.Audit })))
const Docs = lazy(() => import('@/pages/Docs').then((m) => ({ default: m.Docs })))
const SeanerBus = lazy(() => import('./pages/SeanerBus'))
const Approvals = lazy(() => import('@/pages/Approvals').then((m) => ({ default: m.Approvals })))
const Pipelines = lazy(() => import('@/pages/Pipelines').then((m) => ({ default: m.Pipelines })))
const Jupyter = lazy(() => import('@/pages/Jupyter').then((m) => ({ default: m.Jupyter })))
const Drift = lazy(() => import('@/pages/Drift').then((m) => ({ default: m.Drift })))
const MlopsConsole = lazy(() => import('@/pages/MlopsConsole').then((m) => ({ default: m.MlopsConsole })))
const FacilityConsole = lazy(() =>
  import('@/pages/FacilityConsole').then((m) => ({ default: m.FacilityConsole })),
)
const Finops = lazy(() => import('@/pages/Finops').then((m) => ({ default: m.Finops })))
const SelfObs = lazy(() => import('@/pages/SelfObs').then((m) => ({ default: m.SelfObs })))
const Governance = lazy(() => import('@/pages/Governance').then((m) => ({ default: m.Governance })))
const Llmops = lazy(() => import('@/pages/Llmops').then((m) => ({ default: m.Llmops })))
const Gateway = lazy(() => import('@/pages/Gateway').then((m) => ({ default: m.Gateway })))
const Prompts = lazy(() => import('@/pages/Prompts').then((m) => ({ default: m.Prompts })))
const Autopilot = lazy(() => import('@/pages/Autopilot').then((m) => ({ default: m.Autopilot })))
const Slo = lazy(() => import('@/pages/Slo').then((m) => ({ default: m.Slo })))
const Scaling = lazy(() => import('@/pages/Scaling').then((m) => ({ default: m.Scaling })))
const Admission = lazy(() => import('@/pages/Admission').then((m) => ({ default: m.Admission })))
const Secrets = lazy(() => import('@/pages/Secrets').then((m) => ({ default: m.Secrets })))
const Features = lazy(() => import('@/pages/Features').then((m) => ({ default: m.Features })))
const Fairness = lazy(() => import('@/pages/Fairness').then((m) => ({ default: m.Fairness })))
const Alerts = lazy(() => import('@/pages/Alerts').then((m) => ({ default: m.Alerts })))
const Flags = lazy(() => import('@/pages/Flags').then((m) => ({ default: m.Flags })))
const NocWall = lazy(() => import('@/pages/NocWall').then((m) => ({ default: m.NocWall })))
const Preferences = lazy(() => import('@/pages/Preferences').then((m) => ({ default: m.Preferences })))
const Projects = lazy(() => import('@/pages/Projects').then((m) => ({ default: m.Projects })))
const ProjectDetail = lazy(() => import('@/pages/ProjectDetail').then((m) => ({ default: m.ProjectDetail })))
const NextGen = lazy(() => import('@/pages/NextGen').then((m) => ({ default: m.NextGen })))
const Providers = lazy(() => import('@/pages/Providers').then((m) => ({ default: m.Providers })))
const Events = lazy(() => import('@/pages/Events').then((m) => ({ default: m.Events })))

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Centralized retry policy (F23): never retry a 4xx client error (see lib/errors.ts).
      retry: (failureCount, error) => shouldRetry(failureCount, error, 1),
      staleTime: 10_000,
    },
  },
})

// Client "301" for the clean-slate URL migration (ADR 0097 §3): redirect an old flat path — and any
// sub-path (e.g. /models/JPCP) — to its new lifecycle-scoped home, preserving the remainder.
export function RedirectSplat({ to }: { to: string }) {
  const rest = useParams()['*']
  return <Navigate to={rest ? `${to}/${rest}` : to} replace />
}

function RouteFallback() {
  return (
    <div className="p-6 space-y-3 max-w-5xl mx-auto">
      <Skeleton className="h-8 w-48" />
      <Skeleton className="h-40 w-full" />
    </div>
  )
}

// Honour the user's default-landing preference on the index route (F21 R3). Fresh users keep '/'.
function Home() {
  const { prefs } = usePrefs()
  return prefs.defaultLanding && prefs.defaultLanding !== '/' ? (
    <Navigate to={prefs.defaultLanding} replace />
  ) : (
    <Overview />
  )
}

// Announce route changes politely so screen-reader users hear the new page without focus theft (F18 R3).
function RouteAnnouncer() {
  const { pathname } = useLocation()
  const { announce } = useAnnouncer()
  useEffect(() => {
    // Announce the leaf segment (the console), not the lifecycle-group prefix — `/build/models` → "Models".
    const seg = pathname === '/' ? 'overview' : (pathname.split('/').filter(Boolean).pop() ?? 'overview')
    announce(`${seg.charAt(0).toUpperCase()}${seg.slice(1)} page`)
  }, [pathname, announce])
  return null
}

export default function App() {
  return (
    <I18nProvider>
      <ThemeProvider>
        <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <AnnouncerProvider>
            <RouteAnnouncer />
            <AuthGate>
              <Layout>
              <div
                className="h-full"
                style={{
                  background:
                    'radial-gradient(ellipse 70% 40% at 80% 0%, oklch(0.64 0.20 265 / 5%) 0%, transparent 60%),' +
                    'radial-gradient(ellipse 40% 30% at 10% 80%, oklch(0.70 0.18 300 / 4%) 0%, transparent 55%)',
                }}
              >
                {/* One page's crash shows a designed fallback instead of blanking the shell (F23). */}
                <ErrorBoundary>
                  <Suspense fallback={<RouteFallback />}>
                    <Routes>
                      {/* Canonical lifecycle-scoped routes (ADR 0097 §1/§3). */}
                      <Route path="/" element={<Home />} />
                      {/* Build */}
                      <Route path="/build/models" element={<Models />} />
                      <Route path="/build/models/:name" element={<ModelDetail />} />
                      <Route path="/build/datasets" element={<Datasets />} />
                      <Route path="/build/pipelines" element={<Pipelines />} />
                      <Route path="/build/prompts" element={<Prompts />} />
                      <Route path="/build/features" element={<Features />} />
                      {/* Serve */}
                      <Route path="/serve/llmops" element={<Llmops />} />
                      <Route path="/serve/gateway" element={<Gateway />} />
                      <Route path="/serve/scaling" element={<Scaling />} />
                      <Route path="/serve/nextgen" element={<NextGen />} />
                      {/* Operate */}
                      <Route path="/operate/drift" element={<Drift />} />
                      <Route path="/operate/alerts" element={<Alerts />} />
                      <Route path="/operate/autopilot" element={<Autopilot />} />
                      <Route path="/operate/slos" element={<Slo />} />
                      <Route path="/operate/admission" element={<Admission />} />
                      <Route path="/operate/finops" element={<Finops />} />
                      <Route path="/operate/self-obs" element={<SelfObs />} />
                      {/* Govern */}
                      <Route path="/govern/compliance" element={<Governance />} />
                      <Route path="/govern/audit" element={<Audit />} />
                      <Route path="/govern/approvals" element={<Approvals />} />
                      <Route path="/govern/secrets" element={<Secrets />} />
                      <Route path="/govern/fairness" element={<Fairness />} />
                      {/* Platform */}
                      <Route path="/platform/events" element={<Events />} />
                      <Route path="/platform/services" element={<Services />} />
                      <Route path="/platform/providers" element={<Providers />} />
                      <Route path="/platform/config" element={<Config />} />
                      <Route path="/platform/integrations" element={<SeanerBus />} />
                      <Route path="/platform/jupyter" element={<Jupyter />} />
                      <Route path="/platform/flags" element={<Flags />} />
                      {/* Feature-flagged consoles (F23 R7 / F25). */}
                      {isEnabled('mlopsConsole') && (
                        <Route path="/build/mlops" element={<MlopsConsole />} />
                      )}
                      {isEnabled('facilityConsole') && (
                        <Route path="/operate/facility" element={<FacilityConsole />} />
                      )}
                      {isEnabled('projectsConsole') && (
                        <>
                          <Route path="/platform/projects" element={<Projects />} />
                          <Route path="/platform/projects/:name" element={<ProjectDetail />} />
                        </>
                      )}
                      {/* Utility (footer) — kept at their existing paths. */}
                      <Route path="/preferences" element={<Preferences />} />
                      <Route path="/documents" element={<Docs />} />
                      {/* NOC/wall kiosk — fixed full-screen overlay; renders inside the authed app so it
                          never drops to a login (F20 R2). */}
                      <Route path="/noc" element={<NocWall />} />
                      {/* Clean-slate URL migration: 301-style redirects from every old flat path (and
                          any sub-path) to its new home, for one release (ADR 0097 §3). */}
                      {Object.entries(ROUTE_REDIRECTS).map(([from, to]) => (
                        <Route key={from} path={`${from}/*`} element={<RedirectSplat to={to} />} />
                      ))}
                    </Routes>
                  </Suspense>
                </ErrorBoundary>
              </div>
              </Layout>
            </AuthGate>
          </AnnouncerProvider>
        </BrowserRouter>
        </QueryClientProvider>
      </ThemeProvider>
    </I18nProvider>
  )
}
