import { useState } from 'react'
import { Lock } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { isAdmin } from '@/lib/auth'
import { useSecrets, useSetSecret } from '@/lib/secrets'

/**
 * Secrets console (D7, dashboard-rebuild M3) — set/rotate platform secrets. Values are WRITE-ONLY:
 * this UI only ever sends a value to the shared `examlops.secrets` store (encrypted, audited) and
 * displays metadata (path/version/who/when) — it can never read a secret back. There is deliberately
 * no reveal control.
 */
export function Secrets() {
  const admin = isAdmin()
  const { data: secrets = [], isLoading, error } = useSecrets()
  const setSecret = useSetSecret()
  const [path, setPath] = useState('')
  const [value, setValue] = useState('')
  const [msg, setMsg] = useState<string | null>(null)

  const save = async () => {
    setMsg(null)
    if (!path.trim() || !value) {
      setMsg('Path and value are required.')
      return
    }
    try {
      const r = await setSecret.mutateAsync({ path: path.trim(), value })
      setValue('') // never keep the plaintext around after sending
      setMsg(`Saved ${r.path} (v${r.version}). The value is encrypted at rest and not retrievable here.`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to save secret')
    }
  }

  return (
    <div className="p-6 space-y-6 max-w-4xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Lock className="size-6 text-muted-foreground" aria-hidden="true" />
          Secrets
        </h1>
        <p className="text-sm text-muted-foreground">
          Platform secrets, encrypted at rest. Values are write-only — they can be set here but never
          read back through the dashboard.
        </p>
      </div>

      {msg && (
        <p className="text-xs rounded-lg px-3 py-2" style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-2)' }}>
          {msg}
        </p>
      )}

      {admin && (
        <section className="space-y-2">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Set a secret</h2>
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-xs text-muted-foreground">
              Path
              <input aria-label="Secret path" value={path} onChange={(e) => setPath(e.target.value)}
                placeholder="svc/api-token" className="mt-1 block w-56 rounded-md border border-border bg-transparent px-2 py-1 text-xs font-mono" />
            </label>
            <label className="text-xs text-muted-foreground">
              Value
              <input aria-label="Secret value" type="password" value={value} onChange={(e) => setValue(e.target.value)}
                autoComplete="new-password" className="mt-1 block w-56 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <button onClick={save} disabled={setSecret.isPending}
              className="rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50">
              Save secret
            </button>
          </div>
        </section>
      )}

      {error && <EmptyState title="Couldn't load secrets" description="The secrets endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-24 w-full" />}
      {!isLoading && secrets.length === 0 && !error && (
        <EmptyState title="No secrets set" description={admin ? 'Set one above.' : 'An admin can set platform secrets here.'} />
      )}

      {secrets.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
                <th className="px-3 py-2 font-medium">Path</th>
                <th className="px-3 py-2 font-medium">Tenant</th>
                <th className="px-3 py-2 font-medium">Version</th>
                <th className="px-3 py-2 font-medium">Updated by</th>
                <th className="px-3 py-2 font-medium">Updated</th>
              </tr>
            </thead>
            <tbody>
              {secrets.map((s) => (
                <tr key={`${s.tenant}/${s.path}`} className="border-b border-border/50">
                  <td className="px-3 py-2 font-mono text-xs">{s.path}</td>
                  <td className="px-3 py-2 text-xs">{s.tenant}</td>
                  <td className="px-3 py-2 text-xs">v{s.version}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">{s.updated_by ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground">{s.updated_at?.slice(0, 19) ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
