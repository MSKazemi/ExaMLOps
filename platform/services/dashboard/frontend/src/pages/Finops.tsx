import { DollarSign, Cpu, Leaf, Receipt } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { KpiTile } from '@/components/viz'
import { DataGrid } from '@/components/DataGrid'
import type { Column } from '@/lib/datagrid'
import { formatHpcUnit } from '@/lib/i18n'
import { useI18n } from '@/hooks/i18nContext'
import { useFinops, usd, budgetPct, carbonLabel, type BudgetRow, type CostRow } from '@/lib/finops'

// Cost-by-model grid columns — sortable + CSV-exportable via the shared <DataGrid/> (F17).
const COST_COLUMNS: Column<CostRow>[] = [
  { key: 'key', header: 'Model', accessor: (r) => r.key, sortable: true },
  { key: 'gpuHours', header: 'GPU-hours', accessor: (r) => r.gpuHours, sortable: true },
  { key: 'runs', header: 'Runs', accessor: (r) => r.runs, sortable: true },
  { key: 'costUsd', header: 'Cost', accessor: (r) => r.costUsd, render: (r) => usd(r.costUsd), sortable: true },
]

function Budgets({ budgets }: { budgets: BudgetRow[] }) {
  if (budgets.length === 0) {
    return <EmptyState title="No budgets set" description="Configure with exa finops budget set <project>." />
  }
  return (
    <div className="space-y-2">
      {budgets.map((b) => (
        <div
          key={b.project}
          className="flex items-center justify-between rounded-lg border border-border px-4 py-2 text-sm"
        >
          <span className="font-medium">{b.project}</span>
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span>GPU-h {budgetPct(b.gpuHoursRatio)}</span>
            <span>Cost {budgetPct(b.costRatio)}</span>
            <StatusPill status={b.overBudget ? 'critical' : 'ok'} label={b.overBudget ? 'Over budget' : 'Within'} />
          </div>
        </div>
      ))}
    </div>
  )
}

export function Finops() {
  const { t, locale } = useI18n()
  const { data, isLoading, error } = useFinops()
  const cost = data?.cost
  const budget = data?.budget
  const carbon = data?.carbon
  const unit = data?.unitEconomics
  const partial = data?._partial ?? []

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <DollarSign className="size-6 text-muted-foreground" aria-hidden="true" />
          {t('finops.title')}
        </h1>
        <p className="text-sm text-muted-foreground">
          GPU-hour spend, budgets, and carbon accounting — sourced from the platform&apos;s cost/carbon backend.
        </p>
      </div>

      {partial.length > 0 && (
        <StatusPill status="warn" label={`Partial data — ${partial.join(', ')} unavailable`} />
      )}

      {isLoading && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3" aria-label="Loading FinOps">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-20 w-full" />
          ))}
        </div>
      )}

      {error && (
        <EmptyState
          title="Couldn't load FinOps data"
          description="The FinOps BFF endpoint is unreachable. Check the platform database."
        />
      )}

      {data && (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <KpiTile label={t('finops.total_spend')} value={usd(cost?.total_cost_usd ?? 0)} icon={DollarSign} />
            <KpiTile
              label={t('finops.gpu_hours')}
              value={formatHpcUnit(cost?.total_gpu_hours ?? 0, 'GPU-h', locale)}
              icon={Cpu}
            />
            <KpiTile
              label={t('finops.carbon')}
              value={carbon ? carbonLabel(carbon.co2e_kg, carbon.uncertainty) : '—'}
              icon={Leaf}
            />
            <KpiTile
              label="Cost / training run"
              value={usd(unit?.costPerTrainingRun ?? null)}
              icon={Receipt}
            />
          </div>

          <section className="space-y-3">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
              {t('finops.cost_by_model')}
            </h2>
            {cost && cost.rows.length > 0 ? (
              <DataGrid
                columns={COST_COLUMNS}
                rows={cost.rows}
                getRowId={(r) => r.key}
                initialSort={[{ col: 'costUsd', dir: 'desc' }]}
                storageKey="finops-cost"
                label="cost-by-model"
              />
            ) : (
              <EmptyState title="No cost records yet" description="Record with exa models cost <model> --record." />
            )}
          </section>

          <section className="space-y-3">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">
              Budgets
            </h2>
            <Budgets budgets={budget?.budgets ?? []} />
          </section>

          {carbon && (
            <p className="text-xs text-muted-foreground border-l-2 border-border pl-3">
              <Leaf className="inline size-3 mr-1" aria-hidden="true" />
              {carbon.methodology}
            </p>
          )}
        </>
      )}
    </div>
  )
}
