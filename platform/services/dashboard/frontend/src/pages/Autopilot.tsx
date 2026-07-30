import { useState } from 'react'
import { Bot, Power } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { isAdmin } from '@/lib/auth'
import { useAutopilotStatus, useSetAutopilotEnabled } from '@/lib/autopilot'

/**
 * Autopilot console (ADR 0085, dashboard-rebuild M3) — the self-driving loop's kill-switch. Admins
 * flip the persistent enable/disable via the shared `examlops.data.autopilot` path (audited). If
 * `EXAMLOPS_AUTOPILOT_ENABLED` is set it overrides at runtime (surfaced here). Running a cycle needs
 * retrain/promote infra and is not exposed.
 */
export function Autopilot() {
  const admin = isAdmin()
  const { data, isLoading, error } = useAutopilotStatus()
  const setEnabled = useSetAutopilotEnabled()
  const [msg, setMsg] = useState<string | null>(null)

  const toggle = async () => {
    if (!data) return
    setMsg(null)
    try {
      await setEnabled.mutateAsync(!data.enabled)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to change autopilot state')
    }
  }

  return (
    <div className="p-6 space-y-6 max-w-4xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Bot className="size-6 text-muted-foreground" aria-hidden="true" />
          Autopilot
        </h1>
        <p className="text-sm text-muted-foreground">
          The self-driving loop (detect → retrain → validate → promote). The kill-switch below is the
          persistent enable/disable; it is off by default.
        </p>
      </div>

      {error && <EmptyState title="Couldn't load autopilot status" description="The autopilot endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-24 w-full" />}
      {msg && (
        <p className="text-sm rounded-lg px-4 py-3" style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
          {msg}
        </p>
      )}

      {data && (
        <>
          <div className="rounded-lg border border-border p-4 flex items-center justify-between gap-4">
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">Kill-switch</span>
                <StatusPill status={data.enabled ? 'ok' : 'warn'} label={data.enabled ? 'Enabled' : 'Disabled'} />
              </div>
              {data.envOverride !== null && (
                <p className="text-xs" style={{ color: 'var(--warning-text)' }}>
                  Overridden at runtime by <code>EXAMLOPS_AUTOPILOT_ENABLED</code> → effective:{' '}
                  <strong>{data.effective ? 'enabled' : 'disabled'}</strong>
                </p>
              )}
            </div>
            {admin ? (
              <button
                onClick={toggle}
                disabled={setEnabled.isPending}
                aria-label={data.enabled ? 'Disable autopilot' : 'Enable autopilot'}
                className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium disabled:opacity-50"
                style={
                  data.enabled
                    ? { background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 28%)', color: 'var(--error-text)' }
                    : { background: 'oklch(0.72 0.18 155 / 15%)', border: '1px solid oklch(0.72 0.18 155 / 35%)', color: 'var(--success-text)' }
                }
              >
                <Power className="size-4" aria-hidden="true" /> {data.enabled ? 'Disable' : 'Enable'}
              </button>
            ) : (
              <span className="text-xs text-muted-foreground">Admin only</span>
            )}
          </div>

          <section className="space-y-3">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">Recent runs</h2>
            {data.recentRuns.length === 0 ? (
              <EmptyState title="No autopilot runs yet" description="Runs appear here once the loop executes a cycle." />
            ) : (
              <div className="overflow-x-auto rounded-lg border border-border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
                      <th className="px-3 py-2 font-medium">When</th>
                      <th className="px-3 py-2 font-medium">Trigger</th>
                      <th className="px-3 py-2 font-medium">Retrains</th>
                      <th className="px-3 py-2 font-medium">Promotions</th>
                      <th className="px-3 py-2 font-medium">Blocks</th>
                      <th className="px-3 py-2 font-medium">HITL</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.recentRuns.map((r) => (
                      <tr key={r.id} className="border-b border-border/50">
                        <td className="px-3 py-2 font-mono text-xs text-muted-foreground">{r.run_at?.slice(0, 19) ?? '—'}</td>
                        <td className="px-3 py-2 text-xs">{r.triggered_by}{r.dry_run ? ' (dry-run)' : ''}</td>
                        <td className="px-3 py-2 text-xs">{r.retrains_triggered}</td>
                        <td className="px-3 py-2 text-xs">{r.promotions_made}</td>
                        <td className="px-3 py-2 text-xs">{r.policy_blocks}</td>
                        <td className="px-3 py-2 text-xs">{r.human_required}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  )
}
