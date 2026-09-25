import { useState } from 'react'
import { KeyRound, Copy, Ban, Send } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { isAdmin } from '@/lib/auth'
import { useVirtualKeys, useIssueKey, useRevokeKey, useGatewayStatus, useTestChat } from '@/lib/gateway'

/**
 * Gateway console (B2, dashboard-rebuild M2) — issue/revoke LLM virtual keys. Writes reuse the shared
 * `examlops.gateway` path (audited). The raw key is shown ONCE on issue and never stored/re-fetchable;
 * the list shows only the hash + scope.
 */
export function Gateway() {
  const admin = isAdmin()
  const { data: keys = [], isLoading, error } = useVirtualKeys()
  const issue = useIssueKey()
  const revoke = useRevokeKey()
  const [project, setProject] = useState('default')
  const [models, setModels] = useState('')
  const [budget, setBudget] = useState('')
  const [issued, setIssued] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  const { data: status, isLoading: statusLoading } = useGatewayStatus()
  const testChat = useTestChat()
  const [testMessage, setTestMessage] = useState('hello')
  const [testRoute, setTestRoute] = useState('default')
  const [testKey, setTestKey] = useState('')
  const [testResult, setTestResult] = useState<Awaited<ReturnType<typeof testChat.mutateAsync>> | null>(null)

  const doTestChat = async () => {
    setTestResult(null)
    try {
      const r = await testChat.mutateAsync({
        message: testMessage,
        route: testRoute.trim() || 'default',
        key: testKey.trim() || undefined,
      })
      setTestResult(r)
    } catch (e) {
      setTestResult({ ok: false, status: null, latencyMs: 0, error: e instanceof Error ? e.message : 'Request failed' })
    }
  }

  const doIssue = async () => {
    setMsg(null)
    setIssued(null)
    try {
      const body = {
        project: project.trim() || 'default',
        models: models.trim() ? models.split(',').map((m) => m.trim()).filter(Boolean) : undefined,
        budgetUsd: budget.trim() ? Number(budget) : undefined,
      }
      const r = await issue.mutateAsync(body)
      setIssued(r.key)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to issue key')
    }
  }
  const doRevoke = async (hash: string) => {
    if (!window.confirm('Revoke this virtual key? Clients using it will be denied immediately.')) return
    setMsg(null)
    try {
      await revoke.mutateAsync(hash)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to revoke key')
    }
  }

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <KeyRound className="size-6 text-muted-foreground" aria-hidden="true" />
          Gateway — Virtual Keys
        </h1>
        <p className="text-sm text-muted-foreground">
          Scoped, budgeted API keys for the LLM gateway. Keys are shown once on issue; only their hash
          is stored.
        </p>
      </div>

      {/* Live status — the deployed gateway's own /ready, no admin credential involved. */}
      <section className="space-y-2">
        <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Live status</h2>
        {statusLoading && <Skeleton className="h-8 w-48" />}
        {!statusLoading && status && (
          <div className="flex flex-wrap items-center gap-3">
            <StatusPill
              status={!status.reachable ? 'critical' : status.ready ? 'ok' : 'warn'}
              label={!status.reachable ? 'Unreachable' : status.ready ? 'Ready' : 'Not ready'}
            />
            {status.reachable && !status.healthyNow && status.ready && (
              <span className="text-xs text-muted-foreground">
                latched ready, but no route is healthy right now
              </span>
            )}
            {Object.keys(status.routes).length > 0 && (
              <span className="text-xs text-muted-foreground">
                {Object.values(status.routes).filter((r) => r.healthy).length}/{Object.keys(status.routes).length} routes healthy
              </span>
            )}
          </div>
        )}
        {!statusLoading && !status?.reachable && (
          <p className="text-xs text-muted-foreground">
            Deployed gateway not reachable from the dashboard. Diagnose deeper provider/route health with{' '}
            <code>exa gateway providers</code> / <code>exa gateway routes</code> on the CLI.
          </p>
        )}
      </section>

      {/* Test chat (admin) — one real message through the deployed gateway; needs a virtual key,
          the same one `exa gateway chat --key` takes. Never stored by the dashboard. */}
      {admin && (
        <section className="space-y-2">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Test chat</h2>
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-xs text-muted-foreground">
              Route
              <input aria-label="Test route" value={testRoute} onChange={(e) => setTestRoute(e.target.value)}
                className="mt-1 block w-32 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <label className="text-xs text-muted-foreground">
              Message
              <input aria-label="Test message" value={testMessage} onChange={(e) => setTestMessage(e.target.value)}
                className="mt-1 block w-64 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <label className="text-xs text-muted-foreground">
              Virtual key (optional)
              <input aria-label="Test virtual key" type="password" value={testKey} onChange={(e) => setTestKey(e.target.value)}
                placeholder="exa-…" className="mt-1 block w-40 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <button onClick={doTestChat} disabled={testChat.isPending || !testMessage.trim()}
              className="inline-flex items-center gap-1 rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50">
              <Send className="size-3" aria-hidden="true" /> Send
            </button>
          </div>
          {testResult && (
            <div
              className="rounded-lg border border-border p-3 text-xs space-y-1"
              style={{ background: testResult.ok ? 'oklch(0.72 0.18 155 / 8%)' : 'oklch(0.66 0.22 25 / 8%)' }}
            >
              <p>
                <StatusPill status={testResult.ok ? 'ok' : 'critical'} label={testResult.ok ? 'ok' : 'failed'} />{' '}
                {testResult.latencyMs.toFixed(0)}ms{testResult.status != null ? ` · HTTP ${testResult.status}` : ''}
              </p>
              {testResult.ok && <p className="font-mono">{testResult.reply}</p>}
              {!testResult.ok && <p style={{ color: 'var(--error-text)' }}>{testResult.error}</p>}
            </div>
          )}
        </section>
      )}

      {msg && (
        <p className="text-sm rounded-lg px-4 py-3" style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
          {msg}
        </p>
      )}

      {/* Raw key — shown ONCE. */}
      {issued && (
        <div className="rounded-lg border border-border p-4 space-y-2" style={{ background: 'oklch(0.72 0.18 155 / 8%)' }}>
          <p className="text-xs font-semibold uppercase tracking-widest" style={{ color: 'var(--success-text)' }}>
            New key — copy it now, it won&apos;t be shown again
          </p>
          <div className="flex items-center gap-2">
            <code className="flex-1 rounded-md border border-border bg-transparent px-2 py-1 text-xs font-mono break-all" aria-label="New virtual key">
              {issued}
            </code>
            <button
              onClick={() => navigator.clipboard?.writeText(issued)}
              aria-label="Copy new key"
              className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-muted"
            >
              <Copy className="size-3" aria-hidden="true" /> Copy
            </button>
          </div>
        </div>
      )}

      {/* Issue form (admin) */}
      {admin && (
        <section className="space-y-2">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Issue a key</h2>
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-xs text-muted-foreground">
              Project
              <input aria-label="Key project" value={project} onChange={(e) => setProject(e.target.value)}
                className="mt-1 block w-40 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <label className="text-xs text-muted-foreground">
              Models (comma-sep, blank = all)
              <input aria-label="Key models" value={models} onChange={(e) => setModels(e.target.value)}
                placeholder="JPCP, MACK" className="mt-1 block w-48 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <label className="text-xs text-muted-foreground">
              Budget $ (optional)
              <input aria-label="Key budget" type="number" value={budget} onChange={(e) => setBudget(e.target.value)}
                className="mt-1 block w-28 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <button onClick={doIssue} disabled={issue.isPending}
              className="rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50">
              Issue key
            </button>
          </div>
        </section>
      )}

      {error && <EmptyState title="Couldn't load virtual keys" description="The gateway endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-12 w-full" />}

      {!isLoading && keys.length === 0 && !error && (
        <EmptyState title="No virtual keys" description={admin ? 'Issue one above to scope gateway access.' : 'An admin can issue scoped gateway keys.'} />
      )}

      {keys.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
                <th className="px-3 py-2 font-medium">Key (hash)</th>
                <th className="px-3 py-2 font-medium">Project</th>
                <th className="px-3 py-2 font-medium">Models</th>
                <th className="px-3 py-2 font-medium">Budget</th>
                <th className="px-3 py-2 font-medium">Spent</th>
                <th className="px-3 py-2 font-medium">Status</th>
                {admin && <th className="px-3 py-2 font-medium text-right">Actions</th>}
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.key_hash} className="border-b border-border/50">
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground" title={k.key_hash}>
                    {k.key_hash.slice(0, 12)}…
                  </td>
                  <td className="px-3 py-2 text-xs">{k.tenant}/{k.project}</td>
                  <td className="px-3 py-2 text-xs">{k.models.length ? k.models.join(', ') : 'all'}</td>
                  <td className="px-3 py-2 text-xs">{k.budget_usd != null ? `$${k.budget_usd}` : '—'}</td>
                  <td className="px-3 py-2 text-xs">${k.spent_usd.toFixed(2)}</td>
                  <td className="px-3 py-2">
                    <StatusPill status={k.revoked ? 'critical' : 'ok'} label={k.revoked ? 'Revoked' : 'Active'} />
                  </td>
                  {admin && (
                    <td className="px-3 py-2 text-right">
                      {!k.revoked && (
                        <button onClick={() => doRevoke(k.key_hash)} disabled={revoke.isPending}
                          aria-label={`Revoke key ${k.key_hash.slice(0, 12)}`}
                          className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium disabled:opacity-50"
                          style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 28%)', color: 'var(--error-text)' }}>
                          <Ban className="size-3" aria-hidden="true" /> Revoke
                        </button>
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
