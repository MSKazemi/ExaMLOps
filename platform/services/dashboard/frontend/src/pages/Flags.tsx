import { Flag } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { useFlagAdmin, useSetFlag, type FlagAdminRow } from '@/lib/serverflags'

function FlagCard({ f }: { f: FlagAdminRow }) {
  const setFlag = useSetFlag()
  const targeting: string[] = []
  if (f.targeting.roles.length) targeting.push(`roles: ${f.targeting.roles.join(', ')}`)
  if (f.targeting.tenants.length) targeting.push(`tenants: ${f.targeting.tenants.join(', ')}`)
  if (f.targeting.percentage !== null) targeting.push(`${f.targeting.percentage}% rollout`)

  return (
    <div className="rounded-lg border border-border p-4 space-y-2">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium flex items-center gap-2">
            {f.name}
            {f.tags.map((t) => (
              <span key={t} className="text-[10px] uppercase tracking-wide rounded bg-muted px-1.5 py-0.5 text-muted-foreground">
                {t}
              </span>
            ))}
          </p>
          <p className="text-xs text-muted-foreground">{f.description}</p>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <StatusPill status={f.effective ? 'ok' : 'warn'} label={f.effective ? 'On' : 'Off'} />
          <button
            type="button"
            onClick={() => setFlag.mutate({ name: f.name, enabled: !f.effective })}
            disabled={setFlag.isPending}
            className="rounded-md border border-border px-2.5 py-1 text-xs hover:bg-muted disabled:opacity-50"
          >
            {f.effective ? 'Disable' : 'Enable'}
          </button>
        </div>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span>default: {f.default ? 'on' : 'off'}</span>
        {f.override !== null && <span>override: {f.override ? 'on' : 'off'}</span>}
        {targeting.map((t) => (
          <span key={t}>{t}</span>
        ))}
      </div>
    </div>
  )
}

export function Flags() {
  const { data, isLoading, error } = useFlagAdmin()

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <Flag className="size-6 text-muted-foreground" aria-hidden="true" />
          Feature Flags
        </h1>
        <p className="text-sm text-muted-foreground">
          Server-evaluated flags with staged rollout. Toggling a flag is audited and applies live.
        </p>
      </div>

      {isLoading && (
        <div className="space-y-2" aria-label="Loading flags">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full" />
          ))}
        </div>
      )}

      {error && <EmptyState title="Couldn't load flags" description="The flags admin endpoint is unreachable or requires admin." />}

      {data && data.flags.length === 0 && !isLoading && (
        <EmptyState title="No feature flags" description="Define flags in the backend registry." />
      )}

      {data && data.flags.length > 0 && (
        <div className="space-y-2">
          {data.flags.map((f) => (
            <FlagCard key={f.name} f={f} />
          ))}
        </div>
      )}
    </div>
  )
}
