import { useState } from 'react'
import { ShieldCheck, FileCheck, ScrollText, Scale } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { isAdmin } from '@/lib/auth'
import { useGovernance, postureToken, coverageLabel, digestShort } from '@/lib/governance'
import {
  useComplianceSystems,
  useClassifySystem,
  useSetConformity,
  RISK_TIERS,
  CONFORMITY_STATES,
} from '@/lib/compliance'

/**
 * EU AI Act system register (ADR 0012) — the editable counterpart to the read-only overview above.
 * Admins can set a system's risk tier and advance its conformity state; both writes go through the
 * dashboard's compliance router → shared `examlops.compliance` path (validated + audited). Reads the
 * `compliance_systems` table the CLI writes (distinct from the legacy overview table).
 */
export function ComplianceRegister() {
  const admin = isAdmin()
  const { data: systems = [], isLoading, error } = useComplianceSystems()
  const classify = useClassifySystem()
  const conformity = useSetConformity()
  const [msg, setMsg] = useState<string | null>(null)
  const [newModel, setNewModel] = useState('')
  const [newTier, setNewTier] = useState<string>('limited')

  const doClassify = async (model: string, riskTier: string) => {
    setMsg(null)
    try {
      await classify.mutateAsync({ model, body: { riskTier } })
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Classification failed')
    }
  }
  const doConformity = async (model: string, state: string) => {
    setMsg(null)
    try {
      await conformity.mutateAsync({ model, state })
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Transition failed')
    }
  }
  const addSystem = async () => {
    if (!newModel.trim()) return
    await doClassify(newModel.trim(), newTier)
    setNewModel('')
  }

  return (
    <section className="space-y-3">
      <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest flex items-center gap-1.5">
        <Scale className="size-3.5" /> EU AI Act — System register{admin ? ' (editable)' : ''}
      </h2>

      {error && <EmptyState title="Couldn't load the system register" description="The compliance endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-12 w-full" />}
      {msg && (
        <p className="text-xs rounded-lg px-3 py-2" style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
          {msg}
        </p>
      )}

      {!isLoading && systems.length === 0 && !admin && (
        <EmptyState title="No systems classified" description="An admin can classify a model's EU-AI-Act risk tier here." />
      )}

      {(systems.length > 0 || admin) && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
                <th className="px-3 py-2 font-medium">Model</th>
                <th className="px-3 py-2 font-medium">Risk tier</th>
                <th className="px-3 py-2 font-medium">Conformity</th>
                <th className="px-3 py-2 font-medium">Updated by</th>
              </tr>
            </thead>
            <tbody>
              {systems.map((s) => (
                <tr key={s.model} className="border-b border-border/50">
                  <td className="px-3 py-2 font-medium font-mono text-xs">{s.model}</td>
                  <td className="px-3 py-2">
                    {admin ? (
                      <select
                        aria-label={`Risk tier for ${s.model}`}
                        value={s.risk_tier ?? ''}
                        onChange={(e) => doClassify(s.model, e.target.value)}
                        className="rounded-md border border-border bg-transparent px-2 py-1 text-xs"
                      >
                        <option value="" disabled>unset</option>
                        {RISK_TIERS.map((t) => <option key={t} value={t}>{t}</option>)}
                      </select>
                    ) : (
                      <span className="text-muted-foreground">{s.risk_tier ?? '—'}</span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {admin ? (
                      <select
                        aria-label={`Conformity state for ${s.model}`}
                        value={s.conformity_state}
                        onChange={(e) => doConformity(s.model, e.target.value)}
                        className="rounded-md border border-border bg-transparent px-2 py-1 text-xs"
                      >
                        {CONFORMITY_STATES.map((st) => <option key={st} value={st}>{st}</option>)}
                      </select>
                    ) : (
                      <StatusPill status={s.conformity_state === 'declared' ? 'ok' : 'warn'} label={s.conformity_state} />
                    )}
                  </td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">{s.updated_by ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {admin && (
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="text"
            value={newModel}
            onChange={(e) => setNewModel(e.target.value)}
            placeholder="Model name…"
            aria-label="New system model name"
            className="w-40 rounded-md border border-border bg-transparent px-2 py-1 text-xs"
          />
          <select
            aria-label="New system risk tier"
            value={newTier}
            onChange={(e) => setNewTier(e.target.value)}
            className="rounded-md border border-border bg-transparent px-2 py-1 text-xs"
          >
            {RISK_TIERS.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <button
            onClick={addSystem}
            disabled={classify.isPending}
            className="rounded-md border border-primary bg-primary px-2 py-1 text-xs text-primary-foreground disabled:opacity-50"
          >
            Classify
          </button>
        </div>
      )}
    </section>
  )
}

export function Governance() {
  const { data, isLoading, error } = useGovernance()
  const posture = data?.posture
  const compliance = data?.compliance
  const cards = data?.cards
  const audit = data?.audit
  const partial = data?._partial ?? []

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <ShieldCheck className="size-6 text-muted-foreground" aria-hidden="true" />
          Governance &amp; Compliance
        </h1>
        <p className="text-sm text-muted-foreground">
          NIST posture, EU AI Act status, model-card coverage, and audit integrity — evidence coverage,
          not certification.
        </p>
      </div>

      {partial.length > 0 && (
        <StatusPill status="warn" label={`Partial data — ${partial.join(', ')} unavailable`} />
      )}

      {isLoading && (
        <div className="space-y-2" aria-label="Loading governance">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
      )}

      {error && (
        <EmptyState title="Couldn't load governance data" description="The governance endpoint is unreachable." />
      )}

      {data && (
        <>
          {/* NIST posture (honest evidence coverage) */}
          <section className="space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
                NIST AI RMF posture
              </h2>
              {posture && (
                <span className="text-xs text-muted-foreground">
                  {posture.satisfied}/{posture.total} satisfied
                </span>
              )}
            </div>
            <div className="space-y-2">
              {posture?.controls.map((c) => (
                <div key={c.control} className="rounded-lg border border-border px-4 py-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium">
                      {c.title}{' '}
                      <span className="text-xs text-muted-foreground font-mono">({c.control})</span>
                    </span>
                    <StatusPill status={postureToken(c.status)} label={c.status} />
                  </div>
                  {c.evidence.length > 0 && (
                    <p className="text-xs text-muted-foreground mt-0.5">{c.evidence.join(' · ')}</p>
                  )}
                </div>
              ))}
            </div>
          </section>

          {/* EU AI Act compliance */}
          <section className="space-y-3">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest flex items-center gap-1.5">
              <FileCheck className="size-3.5" /> EU AI Act
            </h2>
            {compliance && compliance.rows.length > 0 ? (
              <div className="overflow-x-auto rounded-lg border border-border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase tracking-wider text-muted-foreground border-b border-border">
                      <th className="px-3 py-2 font-medium">Model</th>
                      <th className="px-3 py-2 font-medium">Risk class</th>
                      <th className="px-3 py-2 font-medium">Technical file</th>
                      <th className="px-3 py-2 font-medium">Provenance</th>
                    </tr>
                  </thead>
                  <tbody>
                    {compliance.rows.map((r) => (
                      <tr key={r.model} className="border-b border-border/50">
                        <td className="px-3 py-2 font-medium">{r.model}</td>
                        <td className="px-3 py-2 text-muted-foreground">{r.riskClass}</td>
                        <td className="px-3 py-2">
                          <StatusPill status={r.technicalFile ? 'ok' : 'warn'} label={r.technicalFile ? 'Present' : 'Missing'} />
                        </td>
                        <td className="px-3 py-2">
                          <StatusPill status={r.provenance ? 'ok' : 'warn'} label={r.provenance ? 'Signed' : 'None'} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title="No compliance records" description="Use the editable system register below to classify a model." />
            )}
          </section>

          {/* EU AI Act — editable system register (classify + conformity) */}
          <ComplianceRegister />

          {/* Cards + audit integrity */}
          <section className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-lg border border-border p-4 space-y-1">
              <p className="text-xs text-muted-foreground uppercase tracking-widest">Model-card coverage</p>
              <p className="text-2xl font-bold">{coverageLabel(cards?.coverage ?? null)}</p>
              {cards && cards.withoutCard.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  Missing: {cards.withoutCard.join(', ')}
                </p>
              )}
            </div>
            <div className="rounded-lg border border-border p-4 space-y-1">
              <p className="text-xs text-muted-foreground uppercase tracking-widest flex items-center gap-1.5">
                <ScrollText className="size-3.5" /> Audit integrity
              </p>
              <div className="flex items-center gap-2">
                <StatusPill status={audit?.verified ? 'ok' : 'critical'} label={audit?.verified ? 'Chain verified' : 'Tampered'} />
                <span className="text-xs text-muted-foreground">{audit?.count ?? 0} events</span>
              </div>
              <p className="text-xs text-muted-foreground font-mono">digest {digestShort(audit?.headDigest ?? null)}</p>
            </div>
          </section>
        </>
      )}
    </div>
  )
}
