import { useState } from 'react'
import { Split } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { isAdmin } from '@/lib/auth'
import {
  useAb,
  useShadow,
  useStartAbTest,
  useStopAbTest,
  useSetShadow,
  type AbAnalysis,
} from '@/lib/traffic'

/**
 * Traffic console (A/B testing + shadow deployments · Serve group).
 *
 * Surfaces `exa serve ab` and `exa serve shadow` in the UI over pure `platform.db` state (the same
 * `examlops.data` code paths the CLI uses; the Ray traffic actuation is separate and out of scope).
 * Viewers see the A/B tests + recorded analysis and the shadow config + last comparisons; admins
 * start/stop A/B tests and enable/disable shadow (audited `source=dashboard`). Viewers are read-only —
 * write controls render disabled with an explanation (F15 R3).
 */
export function Traffic() {
  const admin = isAdmin()
  const [model, setModel] = useState('JPCP')

  return (
    <div className="p-6 space-y-8 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Split className="size-6 text-muted-foreground" aria-hidden="true" />
          Traffic
        </h1>
        <p className="text-sm text-muted-foreground">
          A/B experiments and shadow deployments over the shared platform state — the same
          bookkeeping <code className="text-xs">exa serve ab</code> and{' '}
          <code className="text-xs">exa serve shadow</code> manage. The live Ray traffic routing is
          driven separately.
        </p>
        {!admin && (
          <p className="text-xs text-muted-foreground">
            Requires the admin role to start/stop tests or change shadow.
          </p>
        )}
      </div>

      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-muted-foreground">
          Model
          <input
            aria-label="Model name"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="JPCP"
            className="mt-1 block w-48 rounded-md border border-border bg-transparent px-2 py-1 text-xs"
          />
        </label>
      </div>

      <AbSection model={model} admin={admin} />
      <ShadowSection model={model} admin={admin} />
    </div>
  )
}

function AbSection({ model, admin }: { model: string; admin: boolean }) {
  const trimmed = model.trim()
  const { data, isLoading, error } = useAb(trimmed || null)
  const startAb = useStartAbTest(trimmed)
  const stopAb = useStopAbTest(trimmed)
  const [variantA, setVariantA] = useState('Production')
  const [variantB, setVariantB] = useState('Canary')
  const [split, setSplit] = useState('50')
  const [msg, setMsg] = useState<string | null>(null)

  const hasRunning = (data?.tests ?? []).some((t) => t.status === 'running')

  const start = async () => {
    setMsg(null)
    const s = Number(split)
    if (!Number.isInteger(s) || s < 0 || s > 100) {
      setMsg('Split must be an integer between 0 and 100.')
      return
    }
    try {
      await startAb.mutateAsync({
        model: trimmed,
        variant_a: variantA.trim() || 'Production',
        variant_b: variantB.trim() || 'Canary',
        split: s,
      })
      setMsg(`Started A/B test for ${trimmed}.`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to start A/B test')
    }
  }

  const stop = async () => {
    setMsg(null)
    try {
      const r = await stopAb.mutateAsync()
      setMsg(r.stopped ? `Stopped A/B test for ${trimmed}.` : `No running A/B test for ${trimmed}.`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to stop A/B test')
    }
  }

  return (
    <section className="space-y-3">
      <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
        A/B testing
      </h2>

      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-muted-foreground">
          Variant A
          <input
            aria-label="Variant A"
            value={variantA}
            onChange={(e) => setVariantA(e.target.value)}
            disabled={!admin}
            className="mt-1 block w-32 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50"
          />
        </label>
        <label className="text-xs text-muted-foreground">
          Variant B
          <input
            aria-label="Variant B"
            value={variantB}
            onChange={(e) => setVariantB(e.target.value)}
            disabled={!admin}
            className="mt-1 block w-32 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50"
          />
        </label>
        <label className="text-xs text-muted-foreground">
          Split % (A)
          <input
            aria-label="Split percent"
            value={split}
            onChange={(e) => setSplit(e.target.value)}
            disabled={!admin}
            className="mt-1 block w-24 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50"
          />
        </label>
        <button
          onClick={start}
          disabled={!admin || !trimmed || startAb.isPending}
          title={admin ? undefined : 'Requires the admin role.'}
          className="rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50"
        >
          Start test
        </button>
        <button
          onClick={stop}
          disabled={!admin || !hasRunning || stopAb.isPending}
          title={admin ? undefined : 'Requires the admin role.'}
          className="rounded-md border border-border px-3 py-1.5 text-xs disabled:opacity-50"
        >
          Stop test
        </button>
      </div>

      {msg && (
        <p
          className="text-xs rounded-lg px-3 py-2"
          style={{
            background: 'var(--surface-1)',
            border: '1px solid var(--border)',
            color: 'var(--text-2)',
          }}
        >
          {msg}
        </p>
      )}

      {error && (
        <EmptyState
          title="Couldn't load A/B tests"
          description="The traffic endpoint is unreachable."
        />
      )}
      {isLoading && <Skeleton className="h-20 w-full" />}

      {data && (
        <>
          {data.tests.length === 0 ? (
            <EmptyState title="No A/B tests" description={`No experiments recorded for ${trimmed}.`} />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-muted-foreground">
                    <th className="py-1 pr-3">Name</th>
                    <th className="py-1 pr-3">Variant A</th>
                    <th className="py-1 pr-3">Variant B</th>
                    <th className="py-1 pr-3">Split</th>
                    <th className="py-1 pr-3">Status</th>
                    <th className="py-1 pr-3">Started</th>
                  </tr>
                </thead>
                <tbody>
                  {data.tests.map((t) => (
                    <tr key={t.id} className="border-t border-border">
                      <td className="py-1 pr-3">{t.name || '—'}</td>
                      <td className="py-1 pr-3">{t.variant_a}</td>
                      <td className="py-1 pr-3">{t.variant_b}</td>
                      <td className="py-1 pr-3">
                        {t.split_pct}/{100 - t.split_pct}%
                      </td>
                      <td className="py-1 pr-3">{t.status}</td>
                      <td className="py-1 pr-3">{(t.started_at || '—').slice(0, 19)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <AnalysisCard analysis={data.analysis} />
        </>
      )}
    </section>
  )
}

function AnalysisCard({ analysis }: { analysis: AbAnalysis | null }) {
  if (!analysis) return null
  const winnerLabel =
    analysis.winner === 'a'
      ? analysis.variant_a
      : analysis.winner === 'b'
        ? analysis.variant_b
        : '—'
  return (
    <div className="rounded-lg border border-border px-3 py-2 text-xs space-y-1">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        Analysis (Welch t-test)
      </div>
      {analysis.verdict === 'insufficient_sample' ? (
        <div>
          Insufficient sample — {analysis.variant_a} n={analysis.n_a}, {analysis.variant_b} n=
          {analysis.n_b} (need ≥ {analysis.min_sample} each).
        </div>
      ) : (
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <span>
            p-value: {analysis.p_value !== undefined ? analysis.p_value.toPrecision(3) : '—'}
          </span>
          <span>significant: {analysis.significant ? 'yes' : 'no'}</span>
          <span>winner: {winnerLabel}</span>
        </div>
      )}
    </div>
  )
}

function ShadowSection({ model, admin }: { model: string; admin: boolean }) {
  const trimmed = model.trim()
  const { data, isLoading, error } = useShadow(trimmed || null)
  const setShadow = useSetShadow(trimmed)
  const [alias, setAlias] = useState('Staging')
  const [msg, setMsg] = useState<string | null>(null)

  const current = data?.config?.[0]

  const apply = async (enabled: boolean) => {
    setMsg(null)
    try {
      await setShadow.mutateAsync({ model: trimmed, enabled, shadow_alias: alias.trim() || 'Staging' })
      setMsg(`Shadow ${enabled ? 'enabled' : 'disabled'} for ${trimmed}.`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to update shadow config')
    }
  }

  return (
    <section className="space-y-3">
      <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
        Shadow deployment
      </h2>

      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-muted-foreground">
          Shadow alias
          <input
            aria-label="Shadow alias"
            value={alias}
            onChange={(e) => setAlias(e.target.value)}
            disabled={!admin}
            className="mt-1 block w-32 rounded-md border border-border bg-transparent px-2 py-1 text-xs disabled:opacity-50"
          />
        </label>
        <button
          onClick={() => apply(true)}
          disabled={!admin || !trimmed || setShadow.isPending}
          title={admin ? undefined : 'Requires the admin role.'}
          className="rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50"
        >
          Enable
        </button>
        <button
          onClick={() => apply(false)}
          disabled={!admin || !trimmed || setShadow.isPending}
          title={admin ? undefined : 'Requires the admin role.'}
          className="rounded-md border border-border px-3 py-1.5 text-xs disabled:opacity-50"
        >
          Disable
        </button>
      </div>

      {current && (
        <div className="text-xs text-muted-foreground">
          Current: alias <span className="text-foreground">{current.shadow_alias}</span> —{' '}
          {current.enabled ? 'enabled' : 'disabled'}
        </div>
      )}

      {msg && (
        <p
          className="text-xs rounded-lg px-3 py-2"
          style={{
            background: 'var(--surface-1)',
            border: '1px solid var(--border)',
            color: 'var(--text-2)',
          }}
        >
          {msg}
        </p>
      )}

      {error && (
        <EmptyState
          title="Couldn't load shadow config"
          description="The traffic endpoint is unreachable."
        />
      )}
      {isLoading && <Skeleton className="h-20 w-full" />}

      {data && data.results.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-muted-foreground">
                <th className="py-1 pr-3">Time</th>
                <th className="py-1 pr-3">Prod pred</th>
                <th className="py-1 pr-3">Shadow pred</th>
                <th className="py-1 pr-3">Diff %</th>
              </tr>
            </thead>
            <tbody>
              {data.results.map((r) => (
                <tr key={r.id} className="border-t border-border">
                  <td className="py-1 pr-3">{(r.ts || '—').slice(0, 19)}</td>
                  <td className="py-1 pr-3">{r.production_pred ?? '—'}</td>
                  <td className="py-1 pr-3">{r.shadow_pred ?? '—'}</td>
                  <td className="py-1 pr-3">
                    {r.diff_pct !== null ? `${r.diff_pct.toFixed(2)}%` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
