import { useState } from 'react'
import { Swords } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { isAdmin } from '@/lib/auth'
import {
  useChallengers,
  useChallengerStatus,
  useDisableChallenger,
  usePromoteChallenger,
} from '@/lib/challenger'

/**
 * Champion-challenger console (ADR 0024 clause 4). The scoreboard, the Welch/z-test engine and
 * `exa serve challenger` all existed; this is the surface that shows the comparison an operator is
 * meant to act on — is the challenger better, is it significant, and does C6 say it is safe.
 *
 * A refused promotion is shown, not hidden: the reason ("not significant", "too few samples", an
 * SLO regression) is the useful half of the answer, and an error toast would throw it away.
 */
export function Challenger() {
  const admin = isAdmin()
  const { data: configs, isLoading, error } = useChallengers()
  const [selected, setSelected] = useState<string | null>(null)
  const { data: status } = useChallengerStatus(selected)
  const promote = usePromoteChallenger()
  const disable = useDisableChallenger()
  const [msg, setMsg] = useState<string | null>(null)

  const onPromote = async (model: string) => {
    setMsg(null)
    try {
      const res = await promote.mutateAsync(model)
      setMsg(
        res.proposed
          ? `Promotion proposed for ${model}: ${res.reason ?? ''}`
          : `Not promoted — ${res.reason ?? 'policy not met'}`,
      )
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Promotion failed')
    }
  }

  const onDisable = async (model: string) => {
    setMsg(null)
    try {
      await disable.mutateAsync(model)
      setMsg(`Challenger disabled for ${model}`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Disable failed')
    }
  }

  const fmt = (v: number | null | undefined, digits = 4) =>
    v === null || v === undefined ? '—' : v.toFixed(digits)

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Swords className="size-6 text-muted-foreground" aria-hidden="true" />
          Champion-Challenger
        </h1>
        <p className="text-sm text-muted-foreground">
          Challengers scored against the live champion. A promotion is proposed only when the
          challenger wins by the configured margin, the result is statistically significant, and
          there is no SLO regression.
        </p>
      </div>

      {error && (
        <EmptyState
          title="Couldn't load challengers"
          description="The challenger endpoint is unreachable."
        />
      )}
      {isLoading && <Skeleton className="h-24 w-full" />}

      {configs && configs.length === 0 && (
        <EmptyState
          title="No challengers configured"
          description="Start one with `exa serve challenger enable <model> <version>`."
        />
      )}

      {configs && configs.length > 0 && (
        <table className="w-full text-sm">
          <caption className="sr-only">Configured challengers</caption>
          <thead>
            <tr className="text-left text-muted-foreground border-b">
              <th scope="col" className="py-2">Model</th>
              <th scope="col">Challenger</th>
              <th scope="col">Mirror %</th>
              <th scope="col">State</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {configs.map((c) => (
              <tr key={c.model} className="border-b last:border-0">
                <td className="py-2">
                  <button
                    className="underline underline-offset-2"
                    onClick={() => setSelected(c.model)}
                  >
                    {c.model}
                  </button>
                </td>
                <td>{c.challenger_version}</td>
                <td>{c.mirror_pct}</td>
                <td>
                  <StatusPill
                    status={c.enabled ? 'healthy' : 'pending'}
                    label={c.enabled ? 'enabled' : 'disabled'}
                  />
                </td>
                <td className="space-x-2">
                  <button
                    className="text-xs underline underline-offset-2 disabled:opacity-50"
                    disabled={!admin}
                    title={admin ? undefined : 'Requires an admin role'}
                    onClick={() => onPromote(c.model)}
                  >
                    Promote
                  </button>
                  <button
                    className="text-xs underline underline-offset-2 disabled:opacity-50"
                    disabled={!admin || !c.enabled}
                    title={admin ? undefined : 'Requires an admin role'}
                    onClick={() => onDisable(c.model)}
                  >
                    Disable
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {status && status.configured && (
        <section className="rounded-lg border p-4 space-y-2">
          <h2 className="font-semibold">{status.model} — scoreboard</h2>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
            <div>
              <dt className="text-muted-foreground">Samples</dt>
              <dd>{status.n ?? '—'}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Champion error</dt>
              <dd>{fmt(status.champion_error)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Challenger error</dt>
              <dd>{fmt(status.challenger_error)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Δ (positive = better)</dt>
              <dd>{fmt(status.delta)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">p-value</dt>
              <dd>{fmt(status.p_value)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Significant</dt>
              <dd>{status.significant ? 'yes' : 'no'}</dd>
            </div>
          </dl>
          <p className="text-sm">
            <span className="text-muted-foreground">SLO: </span>
            <StatusPill
              status={status.slo_ok ? 'healthy' : 'failed'}
              label={status.slo_ok ? 'no regression' : 'regression'}
            />{' '}
            <span className="text-muted-foreground">{status.slo_reason}</span>
          </p>
          <p className="text-sm">
            <span className="text-muted-foreground">Promotion policy: </span>
            <StatusPill
              status={status.policy_met ? 'healthy' : 'warn'}
              label={status.policy_met ? 'met' : 'not met'}
            />
          </p>
        </section>
      )}

      {status && !status.configured && (
        <EmptyState
          title={`No challenger for ${status.model}`}
          description="Nothing is being compared against this model's champion."
        />
      )}

      {msg && <p className="text-sm" role="status">{msg}</p>}
    </div>
  )
}
