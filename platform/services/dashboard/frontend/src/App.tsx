import { lazy, Suspense, useEffect } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'
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
const DataPlane = lazy(() => import('./pages/DataPlane'))
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
const Alerts = lazy(() => import('@/pages/Alerts').then((m) => ({ default: m.Alerts })))
const Flags = lazy(() => import('@/pages/Flags').then((m) => ({ default: m.Flags })))
const NocWall = lazy(() => import('@/pages/NocWall').then((m) => ({ default: m.NocWall })))
const Preferences = lazy(() => import('@/pages/Preferences').then((m) => ({ default: m.Preferences })))
const Projects = lazy(() => import('@/pages/Projects').then((m) => ({ default: m.Projects })))
const ProjectDetail = lazy(() => import('@/pages/ProjectDetail').then((m) => ({ default: m.ProjectDetail })))
const NextGen = lazy(() => import('@/pages/NextGen').then((m) => ({ default: m.NextGen })))

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Centralized retry policy (F23): never retry a 4xx client error (see lib/errors.ts).
      retry: (failureCount, error) => shouldRetry(failureCount, error, 1),
      staleTime: 10_000,
    },
  },
})

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
    const seg = pathname === '/' ? 'overview' : pathname.slice(1).split('/')[0]
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
                      <Route path="/" element={<Home />} />
                      <Route path="/preferences" element={<Preferences />} />
                      <Route path="/services" element={<Services />} />
                      <Route path="/models" element={<Models />} />
                      <Route path="/models/:name" element={<ModelDetail />} />
                      <Route path="/datasets" element={<Datasets />} />
                      <Route path="/config" element={<Config />} />
                      <Route path="/audit" element={<Audit />} />
                      <Route path="/documents" element={<Docs />} />
                      <Route path="/dataplane" element={<DataPlane />} />
                      <Route path="/approvals" element={<Approvals />} />
                      <Route path="/pipelines" element={<Pipelines />} />
                      <Route path="/jupyter" element={<Jupyter />} />
                      <Route path="/drift" element={<Drift />} />
                      {/* New surfaces ship behind feature flags (F23 R7 / F25). */}
                      {isEnabled('mlopsConsole') && (
                        <Route path="/mlops" element={<MlopsConsole />} />
                      )}
                      {isEnabled('facilityConsole') && (
                        <Route path="/facility" element={<FacilityConsole />} />
                      )}
                      {isEnabled('projectsConsole') && (
                        <>
                          <Route path="/projects" element={<Projects />} />
                          <Route path="/projects/:name" element={<ProjectDetail />} />
                        </>
                      )}
                      <Route path="/nextgen" element={<NextGen />} />
                      <Route path="/finops" element={<Finops />} />
                      <Route path="/status" element={<SelfObs />} />
                      <Route path="/governance" element={<Governance />} />
                      <Route path="/llmops" element={<Llmops />} />
                      <Route path="/alerts" element={<Alerts />} />
                      <Route path="/flags" element={<Flags />} />
                      {/* NOC/wall kiosk — fixed full-screen overlay; renders inside the authed app so it
                          never drops to a login (F20 R2). */}
                      <Route path="/noc" element={<NocWall />} />
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
