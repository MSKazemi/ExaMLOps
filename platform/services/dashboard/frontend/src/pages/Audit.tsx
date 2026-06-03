import { ShieldCheck } from 'lucide-react'
import { useAudit } from '@/lib/api'

export function Audit() {
  const { data, isLoading, error } = useAudit(100, 0)

  return (
    <div className="p-6 space-y-6 max-w-4xl mx-auto">
      <div className="flex items-center gap-3">
        <div className="w-9 h-9 rounded-lg flex items-center justify-center"
          style={{ background: 'oklch(0.78 0.18 55 / 12%)', border: '1px solid oklch(0.78 0.18 55 / 30%)' }}>
          <ShieldCheck className="w-4 h-4" style={{ color: 'var(--warning-text)' }} />
        </div>
        <div>
          <h1 className="text-2xl font-bold">Audit log</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Admin secret writes. Values are never recorded.
          </p>
        </div>
      </div>

      {error && (
        <p className="text-sm rounded-lg px-4 py-3"
          style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
          Failed to load audit log.
        </p>
      )}

      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}

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
                <tr><td colSpan={4} className="p-6 text-center text-muted-foreground">No entries.</td></tr>
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
