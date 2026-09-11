import { useState } from 'react'
import { Boxes, ShieldCheck, Split } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { isAdmin } from '@/lib/auth'
import {
  useMlopsRegistry,
  useMlopsPromotion,
  evalGateView,
  type EvalGate,
  promotionVerdict,
  freshnessLabel,
  type ModelRow,
} from '@/lib/mlops'
import {
  useModelTraffic,
  useSetModelTraffic,
  weightSum,
  TRAFFIC_ALIASES,
} from '@/lib/traffic'

// ── promotion panel (guided gate, F9 R4) ──────────────────────────────────────

function PromotionPanel({ name }: { name: string }) {
  const { data, isLoading } = useMlopsPromotion(name)
  if (isLoading) return <Skeleton className="h-24 w-full" />
  if (!data) return null
  const chk = data.promotion
  const verdict = promotionVerdict(chk)
  return (
    <div className="rounded-lg border border-border p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <ShieldCheck className="size-4 text-muted-foreground" aria-hidden="true" />
          <h3 className="text-sm font-semibold">Promotion gate — {chk.model}</h3>
        </div>
        <StatusPill status={verdict.tone === 'ok' ? 'ok' : 'warn'} label={verdict.text} />
      </div>
      {chk.policy.metric && (
        <p className="text-xs text-muted-foreground">
          Policy: promote {chk.policy.fromAlias} → {chk.policy.toAlias} when{' '}
          <code className="font-mono">
            {chk.policy.metric} {chk.policy.operator} {chk.policy.threshold}
          </code>
        </p>
      )}
      {chk.policy.reasons.length > 0 && (
        <ul className="text-xs text-muted-foreground list-disc pl-5 space-y-0.5">
          {chk.policy.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
      <EvalGateSection gate={chk.eval} />
      <p className="text-xs text-muted-foreground">
        Approval: {chk.approval.required ? `required (${chk.approval.state ?? 'pending'})` : 'not required'}
      </p>
    </div>
  )
}

/** The ADR 0008 eval gate, as its latest persisted report found it — never inferred. */
function EvalGateSection({ gate }: { gate: EvalGate }) {
  const view = evalGateView(gate)
  const fmt = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v.toFixed(3))
  return (
    <section aria-label="Eval gate" className="space-y-2">
      <div className="flex items-center gap-2">
        <StatusPill status={view.status} label={view.label} />
        {gate.suite && (
          <span className="text-xs text-muted-foreground">
            suite <code className="font-mono">{gate.suite}</code> vs {gate.baselineAlias} · {gate.mode}
          </span>
        )}
      </div>
      <p className="text-xs text-muted-foreground">{gate.reason}</p>
      {gate.metrics.length > 0 && (
        <table className="w-full text-xs">
          <caption className="sr-only">Eval gate metric verdicts</caption>
          <thead>
            <tr className="text-left text-muted-foreground">
              <th scope="col" className="font-medium">Metric</th>
              <th scope="col" className="font-medium">Candidate</th>
              <th scope="col" className="font-medium">Baseline</th>
              <th scope="col" className="font-medium">Floor</th>
              <th scope="col" className="font-medium">Max drop</th>
              <th scope="col" className="font-medium">Verdict</th>
            </tr>
          </thead>
          <tbody>
            {gate.metrics.map((m) => (
              <tr key={m.name} data-metric={m.name}>
                <td className="font-mono">{m.name}</td>
                <td>{fmt(m.candidate)}</td>
                <td>{fmt(m.baseline)}</td>
                <td>{fmt(m.min)}</td>
                <td>{fmt(m.max_drop)}</td>
                <td>{m.failed ? `fail${m.reason ? ` — ${m.reason}` : ''}` : 'pass'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {gate.lastReport && (
        <p className="text-xs text-muted-foreground">
          Report {gate.lastReport.ts} · v{gate.lastReport.candidate} vs {gate.lastReport.baseline} ·
          aggregate {gate.lastReport.aggregate}
        </p>
      )}
    </section>
  )
}

// ── traffic split editor (exa serve traffic) ─────────────────────────────────

function TrafficPanel({ name }: { name: string }) {
  const admin = isAdmin()
  const { data, isLoading } = useModelTraffic(name)
  const setTraffic = useSetModelTraffic(name)
  const [weights, setWeights] = useState<Record<string, number>>({})
  const [msg, setMsg] = useState<string | null>(null)

  // Seed the editable weights from the loaded rules (or zeros) whenever the model or the
  // underlying rules change. Adjusting state during render (guarded by a seed key) is the
  // React-idiomatic reset — an effect would fight the lint rule and add a render pass.
  const [seedKey, setSeedKey] = useState('')
  const currentKey = `${name}:${data === undefined ? 'loading' : JSON.stringify(data?.rules ?? {})}`
  if (data !== undefined && seedKey !== currentKey) {
    const base: Record<string, number> = {}
    for (const a of TRAFFIC_ALIASES) base[a] = data?.rules?.[a] ?? 0
    for (const [a, v] of Object.entries(data?.rules ?? {})) if (!(a in base)) base[a] = v
    setWeights(base)
    setSeedKey(currentKey)
    setMsg(null)
  }

  const total = weightSum(weights)
  const set = (alias: string, v: string) =>
    setWeights((w) => ({ ...w, [alias]: Math.max(0, Math.min(100, Number(v) || 0)) }))

  const save = async () => {
    setMsg(null)
    // Drop zero-weight aliases (matches the backend) before sending.
    const rules = Object.fromEntries(Object.entries(weights).filter(([, v]) => v > 0))
    try {
      await setTraffic.mutateAsync(rules)
      setMsg('Traffic split updated.')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to update traffic split')
    }
  }

  return (
    <div className="rounded-lg border border-border p-4 space-y-3">
      <div className="flex items-center gap-2">
        <Split className="size-4 text-muted-foreground" aria-hidden="true" />
        <h3 className="text-sm font-semibold">Traffic split — {name}</h3>
      </div>
      {isLoading ? (
        <Skeleton className="h-16 w-full" />
      ) : !admin ? (
        <div className="text-xs text-muted-foreground space-y-1">
          {Object.keys(data?.rules ?? {}).length === 0 ? (
            <p>No traffic split set (100% Production by default).</p>
          ) : (
            Object.entries(data!.rules).map(([a, v]) => (
              <div key={a} className="flex justify-between font-mono">
                <span>{a}</span>
                <span>{v}%</span>
              </div>
            ))
          )}
        </div>
      ) : (
        <div className="space-y-2">
          {Object.keys(weights).map((alias) => (
            <label key={alias} className="flex items-center justify-between gap-2 text-xs">
              <span className="text-muted-foreground">{alias}</span>
              <span className="flex items-center gap-1">
                <input
                  type="number"
                  min={0}
                  max={100}
                  value={weights[alias]}
                  onChange={(e) => set(alias, e.target.value)}
                  className="w-16 rounded-md px-2 py-1 text-right font-mono"
                  style={{ background: 'var(--surface-1)', border: '1px solid var(--border-md)' }}
                />
                <span className="text-muted-foreground">%</span>
              </span>
            </label>
          ))}
          <div className="flex items-center justify-between pt-1">
            <span className="text-xs font-mono" style={{ color: total === 100 ? 'var(--success-text)' : 'var(--error-text)' }}>
              Σ {total}%
            </span>
            <button
              onClick={save}
              disabled={total !== 100 || setTraffic.isPending}
              className="rounded-md px-3 py-1 text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
              style={{ background: 'oklch(0.64 0.20 265)', border: '1px solid oklch(0.64 0.20 265 / 60%)', color: 'oklch(0.99 0 0)' }}
            >
              {setTraffic.isPending ? 'Saving…' : 'Set split'}
            </button>
          </div>
          {msg && <p className="text-xs text-muted-foreground">{msg}</p>}
          <p className="text-[11px] text-muted-foreground font-mono">
            exa serve traffic {name}
            {Object.entries(weights).filter(([, v]) => v > 0).map(([a, v]) => ` --${a.toLowerCase()} ${v}`).join('')}
          </p>
        </div>
      )}
    </div>
  )
}

// ── registry grid (F9 R1) ─────────────────────────────────────────────────────

function RegistryGrid({
  rows,
  selected,
  onSelect,
}: {
  rows: ModelRow[]
  selected: string | null
  onSelect: (name: string) => void
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
            <th className="px-3 py-2 font-medium">Model</th>
            <th className="px-3 py-2 font-medium">Ver</th>
            <th className="px-3 py-2 font-medium">Stage</th>
            <th className="px-3 py-2 font-medium">Health</th>
            <th className="px-3 py-2 font-medium">Governed</th>
            <th className="px-3 py-2 font-medium">Freshness</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.name}
              onClick={() => onSelect(r.mlflowName)}
              aria-selected={selected === r.mlflowName}
              className={`cursor-pointer border-b border-border/50 transition-colors hover:bg-muted/40 ${
                selected === r.mlflowName ? 'bg-muted/60' : ''
              }`}
            >
              <td className="px-3 py-2 font-medium">{r.name}</td>
              <td className="px-3 py-2 text-muted-foreground">{r.version ?? '—'}</td>
              <td className="px-3 py-2 text-muted-foreground">{r.stage}</td>
              <td className="px-3 py-2">
                <StatusPill status={r.health} />
              </td>
              <td className="px-3 py-2">
                {r.governed ? (
                  <StatusPill status="ok" label="Yes" />
                ) : (
                  <StatusPill status="warn" label="No policy" />
                )}
              </td>
              <td className="px-3 py-2 text-xs text-muted-foreground font-mono">
                {freshnessLabel(r.freshness)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── page ───────────────────────────────────────────────────────────────────────

export function MlopsConsole() {
  const { data, isLoading, error } = useMlopsRegistry()
  const [selected, setSelected] = useState<string | null>(null)
  const rows = data?.registry.rows ?? []
  const partial = data?._partial ?? []

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Boxes className="size-6 text-muted-foreground" aria-hidden="true" />
          MLOps Console
        </h1>
        <p className="text-sm text-muted-foreground">
          Model registry, lifecycle health, and guided promotion gates — sourced live from the platform.
        </p>
      </div>

      {partial.length > 0 && (
        <StatusPill status="warn" label={`Partial data — ${partial.join(', ')} unavailable`} />
      )}

      {isLoading && (
        <div className="space-y-2" aria-label="Loading registry">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      )}

      {error && (
        <EmptyState
          title="Couldn't load the registry"
          description="The MLOps BFF endpoint is unreachable. Check the control plane and platform database."
        />
      )}

      {!isLoading && !error && rows.length === 0 && (
        <EmptyState
          title="No models tracked yet"
          description="Once a model records drift, cost, traffic, or a promotion policy, it appears here."
        />
      )}

      {rows.length > 0 && (
        <div className="grid gap-6 lg:grid-cols-[1fr_20rem]">
          <RegistryGrid rows={rows} selected={selected} onSelect={setSelected} />
          <div className="space-y-3">
            {selected ? (
              <>
                <PromotionPanel name={selected} />
                <TrafficPanel name={selected} />
              </>
            ) : (
              <EmptyState
                title="Select a model"
                description="Pick a model to see its promotion gate and traffic split."
              />
            )}
          </div>
        </div>
      )}
    </div>
  )
}
