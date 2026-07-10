import { useState } from 'react'
import { ShieldCheck, RefreshCw } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { useAudit, apiFetch } from '@/lib/api'
import { EmptyState } from '@/components/ui/empty-state'
import { Skeleton } from '@/components/ui/skeleton'

/** Shared loading placeholder (F3 Skeleton convention). */
function TableSkeleton() {
  return (
    <div className="space-y-2" aria-label="Loading">
      {Array.from({ length: 5 }).map((_, i) => (
        <Skeleton key={i} className="h-8 w-full" />
      ))}
    </div>
  )
}

interface PlatformAuditRow {
  id: number
  ts: string
  source: string
  actor: string
  action: string
  target: string | null
  details: string | null
}

interface PlatformAuditResponse {
  items: PlatformAuditRow[]
  total: number
}

function PlatformAuditTab() {
  const { data, isLoading, error, refetch } = useQuery<PlatformAuditResponse>({
    queryKey: ['platform-audit'],
    queryFn: () => apiFetch<PlatformAuditResponse>('/api/platform-audit?last_days=30'),
  })

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">Platform operations audit — last 30 days.</p>
        <button
          onClick={() => refetch()}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs"
          style={{ background: 'var(--surface-1)', color: 'var(--text-2)', border: '1px solid var(--border)' }}
        >
          <RefreshCw size={12} /> Refresh
        </button>
      </div>

      {error && (
        <p className="text-sm rounded-lg px-4 py-3"
          style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
          Failed to load platform audit log.
        </p>
      )}

      {isLoading && <TableSkeleton />}

      {data && (
        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
          <table className="w-full text-sm">
            <thead style={{ background: 'var(--surface-1)' }}>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="p-3">When</th>
                <th className="p-3">Source</th>
                <th className="p-3">Actor</th>
                <th className="p-3">Action</th>
                <th className="p-3">Target</th>
                <th className="p-3">Details</th>
              </tr>
            </thead>
            <tbody style={{ background: 'var(--surface-0)' }}>
              {data.items.length === 0 ? (
                <tr><td colSpan={6} className="p-4"><EmptyState icon={ShieldCheck} title="No entries" className="border-0" /></td></tr>
              ) : data.items.map(row => (
                <tr key={row.id} className="border-t" style={{ borderColor: 'var(--border-sm)' }}>
                  <td className="p-3 font-mono text-xs text-muted-foreground">
                    {new Date(row.ts).toLocaleString()}
                  </td>
                  <td className="p-3 text-xs font-mono">{row.source}</td>
                  <td className="p-3 text-xs">{row.actor}</td>
                  <td className="p-3 text-xs font-medium">{row.action}</td>
                  <td className="p-3 font-mono text-xs text-muted-foreground">{row.target ?? '—'}</td>
                  <td className="p-3 text-xs text-muted-foreground">
                    {row.details ? (row.details.length > 60 ? row.details.slice(0, 60) + '…' : row.details) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="p-3 text-xs text-muted-foreground text-right" style={{ background: 'var(--surface-1)' }}>
            {data.total} total
          </div>
        </div>
      )}
    </div>
  )
}

function ConfigAuditTab() {
  const { data, isLoading, error } = useAudit(100, 0)

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">Admin secret writes. Values are never recorded.</p>

      {error && (
        <p className="text-sm rounded-lg px-4 py-3"
          style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
          Failed to load audit log.
        </p>
      )}

      {isLoading && <TableSkeleton />}

      {data && (
        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
          <table className="w-full text-sm">
            <thead style={{ background: 'var(--surface-1)' }}>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="p-3">When</th>
                <th className="p-3">Role</th>
                <th className="p-3">Action</th>
                <th className="p-3">Key</th>
              </tr>
            </thead>
            <tbody style={{ background: 'var(--surface-0)' }}>
              {data.items.length === 0 ? (
                <tr><td colSpan={4} className="p-4"><EmptyState icon={ShieldCheck} title="No entries" className="border-0" /></td></tr>
              ) : data.items.map(row => (
                <tr key={row.id} className="border-t" style={{ borderColor: 'var(--border-sm)' }}>
                  <td className="p-3 font-mono text-xs text-muted-foreground">
                    {new Date(row.at).toLocaleString()}
                  </td>
                  <td className="p-3">{row.role}</td>
                  <td className="p-3">{row.action}</td>
                  <td className="p-3 font-mono text-xs">{row.key}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="p-3 text-xs text-muted-foreground text-right" style={{ background: 'var(--surface-1)' }}>
            {data.total} total
          </div>
        </div>
      )}
    </div>
  )
}

type AuditTab = 'platform' | 'config'

export function Audit() {
  const [activeTab, setActiveTab] = useState<AuditTab>('platform')

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="flex items-center gap-3">
        <div className="w-9 h-9 rounded-lg flex items-center justify-center"
          style={{ background: 'oklch(0.78 0.18 55 / 12%)', border: '1px solid oklch(0.78 0.18 55 / 30%)' }}>
          <ShieldCheck className="w-4 h-4" style={{ color: 'var(--warning-text)' }} />
        </div>
        <div>
          <h1 className="text-2xl font-bold">Audit log</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Platform operations and config change history.
          </p>
        </div>
      </div>

      {/* Tab bar */}
      <div className="flex gap-1 border-b" style={{ borderColor: 'var(--border)' }}>
        {([
          { id: 'platform' as AuditTab, label: 'Platform Ops' },
          { id: 'config' as AuditTab, label: 'Config Changes' },
        ]).map(({ id, label }) => (
          <button
            key={id}
            onClick={() => setActiveTab(id)}
            className="px-4 py-2 text-sm font-medium transition-colors"
            style={activeTab === id
              ? { borderBottom: '2px solid oklch(0.64 0.20 265)', color: 'oklch(0.64 0.20 265)' }
              : { color: 'var(--muted-foreground)' }}
          >
            {label}
          </button>
        ))}
      </div>

      {activeTab === 'platform' && <PlatformAuditTab />}
      {activeTab === 'config' && <ConfigAuditTab />}
    </div>
  )
}
