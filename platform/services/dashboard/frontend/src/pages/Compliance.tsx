import { useState } from 'react'
import { FileCheck, ShieldAlert, Link2, Save } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { KpiTile } from '@/components/viz/KpiTile'
import { isAdmin } from '@/lib/auth'
import { ComplianceRegister } from '@/pages/Governance'
import {
  SUFFICIENCY,
  useArt12,
  useComplianceSystems,
  useSaveTechnicalFile,
  useTechnicalFile,
  useTechnicalFileVersions,
  type TechnicalFile,
} from '@/lib/compliance'

/**
 * EU AI Act Compliance page (ADR 0012 clause 4).
 *
 * The system register, and for a chosen system its Annex-IV technical file generated live from
 * platform evidence — each section judged for sufficiency (ADR 0110 decision 6): verified, not
 * tamper-evident, insufficient (its integrity check failed) or missing. The page opens with what
 * the file cannot vouch for, because a confident partial pack is worse than one that names its
 * gaps. Admins save a version into the same store `exa compliance declare` rests on.
 */
export function Compliance() {
  const { data: systems = [] } = useComplianceSystems()
  const [picked, setPicked] = useState<string | null>(null)
  const model = picked ?? systems[0]?.model ?? null

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <FileCheck className="size-6 text-muted-foreground" aria-hidden="true" />
          EU AI Act Compliance
        </h1>
        <p className="text-sm text-muted-foreground">
          The system register and each system&apos;s Annex-IV technical file, assembled from live
          evidence and judged for integrity — evidence, not certification.
        </p>
      </div>

      <ComplianceRegister />

      <section className="space-y-3" aria-labelledby="techfile-heading">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2
            id="techfile-heading"
            className="text-xs font-semibold text-muted-foreground uppercase tracking-widest"
          >
            Technical file (Annex IV)
          </h2>
          {systems.length > 0 && (
            <label className="text-sm flex items-center gap-2">
              <span className="text-muted-foreground">System</span>
              <select
                className="rounded-md border bg-background px-2 py-1 text-sm"
                value={model ?? ''}
                onChange={(e) => setPicked(e.target.value)}
                aria-label="System"
              >
                {systems.map((s) => (
                  <option key={s.model} value={s.model}>
                    {s.model}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
        {model ? (
          <TechnicalFilePanel model={model} />
        ) : (
          <EmptyState
            title="No systems in the register"
            description="Classify a system first: exa compliance classify <model> --risk-tier high …"
          />
        )}
      </section>
    </div>
  )
}

function TechnicalFilePanel({ model }: { model: string }) {
  const { data, isLoading, error } = useTechnicalFile(model)
  if (isLoading)
    return (
      <div className="space-y-2" aria-label="Loading technical file">
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-12 w-full" />
        ))}
      </div>
    )
  if (error || !data)
    return (
      <EmptyState
        title="Couldn't generate the technical file"
        description="The compliance endpoint is unreachable."
      />
    )
  return <TechnicalFileView file={data} />
}

export function TechnicalFileView({ file }: { file: TechnicalFile }) {
  const weak = file.sections.filter((s) => s.status !== 'verified')
  return (
    <div className="space-y-5">
      <p className="text-xs text-muted-foreground border-l-2 border-border pl-3">{file.disclaimer}</p>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <KpiTile label="Evidence gaps" value={file.gaps} threshold={{ warn: 1, crit: 1 }} />
        <KpiTile label="Missing" value={file.missing} threshold={{ warn: 1, crit: 1 }} />
        <KpiTile label="Insufficient" value={file.insufficient} threshold={{ warn: 1, crit: 1 }} />
        <KpiTile label="Not tamper-evident" value={file.unverified} />
      </div>

      <div className="rounded-lg border p-3 space-y-1 text-sm">
        <div className="flex items-center gap-2 font-medium">
          <Link2 className="size-4 text-muted-foreground" aria-hidden="true" /> Evidence integrity
        </div>
        <div>
          <span className="text-muted-foreground">Audit hash chain: </span>
          {file.auditChain ?? '—'}
        </div>
        <div>
          <span className="text-muted-foreground">Telemetry anchors: </span>
          {file.telemetryAnchors ?? '—'}
        </div>
      </div>

      {weak.length > 0 && (
        <div className="rounded-lg border border-dashed p-3 space-y-2" role="region" aria-label="Insufficient evidence">
          <div className="flex items-center gap-2 font-medium text-sm">
            <ShieldAlert className="size-4 text-muted-foreground" aria-hidden="true" />
            What this file cannot vouch for
          </div>
          <ul className="space-y-2">
            {weak.map((s) => (
              <li key={s.key} className="text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{s.title}</span>
                  <span className="text-xs text-muted-foreground">{s.annexIv}</span>
                  <StatusPill status={SUFFICIENCY[s.status].pill} label={SUFFICIENCY[s.status].label} />
                </div>
                {s.reasons.length > 0 && (
                  <ul className="mt-1 ml-4 list-disc text-xs text-muted-foreground">
                    {s.reasons.map((r) => (
                      <li key={r}>{r}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="space-y-2">
        {file.sections.map((s) => (
          <details key={s.key} className="rounded-lg border p-3">
            <summary className="flex flex-wrap items-center gap-2 cursor-pointer text-sm">
              <span className="font-medium">{s.title}</span>
              <span className="text-xs text-muted-foreground">{s.annexIv}</span>
              <StatusPill status={SUFFICIENCY[s.status].pill} label={SUFFICIENCY[s.status].label} />
            </summary>
            <pre className="mt-2 whitespace-pre-wrap text-xs text-muted-foreground">{s.content}</pre>
          </details>
        ))}
      </div>

      <Art12Panel model={file.model} />
      <VersionsPanel model={file.model} />
    </div>
  )
}

function Art12Panel({ model }: { model: string }) {
  const { data } = useArt12(model)
  if (!data) return null
  return (
    <div className="space-y-2">
      <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
        Art. 12 record-keeping
      </h3>
      <div className="flex flex-wrap gap-2">
        {Object.entries(data.coverage).map(([event, covered]) => (
          <StatusPill
            key={event}
            status={covered ? 'healthy' : 'warn'}
            label={`${event}: ${covered ? 'recorded' : 'not recorded'}`}
          />
        ))}
      </div>
    </div>
  )
}

function VersionsPanel({ model }: { model: string }) {
  const { data: versions = [] } = useTechnicalFileVersions(model)
  const save = useSaveTechnicalFile()
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
          Saved versions
        </h3>
        {isAdmin() && (
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-sm hover:bg-muted disabled:opacity-50"
            onClick={() => save.mutate(model)}
            disabled={save.isPending}
          >
            <Save className="size-4" aria-hidden="true" /> Save version
          </button>
        )}
      </div>
      {save.isError && <StatusPill status="error" label="Save failed" />}
      {versions.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No saved version yet. A declaration of conformity rests on a saved version with no gaps.
        </p>
      ) : (
        <ul className="text-sm space-y-1">
          {versions.map((v) => (
            <li key={v.version} className="flex flex-wrap items-center gap-2">
              <span className="font-medium">v{v.version}</span>
              <StatusPill
                status={v.gaps ? 'warn' : 'healthy'}
                label={v.gaps ? `${v.gaps} gap(s)` : 'no gaps'}
              />
              <span className="text-xs text-muted-foreground">
                {v.generated_at} {v.generated_by ? `· ${v.generated_by}` : ''}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
