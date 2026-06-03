import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '@/lib/theme'
import { AuthGate } from '@/components/AuthGate'
import { Layout } from '@/components/Layout'
import { Overview } from '@/pages/Overview'
import { Services } from '@/pages/Services'
import { Models } from '@/pages/Models'
import { ModelDetail } from '@/pages/ModelDetail'
import { Datasets } from '@/pages/Datasets'
import { Config } from '@/pages/Config'
import { Audit } from '@/pages/Audit'
import { Docs } from '@/pages/Docs'
import SeanerBus from './pages/SeanerBus'
import { Approvals } from '@/pages/Approvals'
import { Pipelines } from '@/pages/Pipelines'
import { Jupyter } from '@/pages/Jupyter'

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 10_000 } },
})

export default function App() {
  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
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
                <Routes>
                  <Route path="/" element={<Overview />} />
                  <Route path="/services" element={<Services />} />
                  <Route path="/models" element={<Models />} />
                  <Route path="/models/:name" element={<ModelDetail />} />
                  <Route path="/datasets" element={<Datasets />} />
                  <Route path="/config" element={<Config />} />
                  <Route path="/audit" element={<Audit />} />
                  <Route path="/docs" element={<Docs />} />
                  <Route path="/seanerbus" element={<SeanerBus />} />
                  <Route path="/approvals" element={<Approvals />} />
                  <Route path="/pipelines" element={<Pipelines />} />
                  <Route path="/jupyter" element={<Jupyter />} />
                </Routes>
              </div>
            </Layout>
          </AuthGate>
        </BrowserRouter>
      </QueryClientProvider>
    </ThemeProvider>
  )
}
