import { useState } from 'react'
import { DollarSign, Cpu, Leaf, Receipt, Pencil } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { KpiTile } from '@/components/viz'
import { DataGrid } from '@/components/DataGrid'
import type { Column } from '@/lib/datagrid'
import { formatHpcUnit } from '@/lib/i18n'
import { useI18n } from '@/hooks/i18nContext'
import { isAdmin } from '@/lib/auth'
import { useUpdateProject, type UpdateProjectBody } from '@/lib/projects'
import { useFinops, usd, budgetPct, carbonLabel, type BudgetRow, type CostRow } from '@/lib/finops'

// Cost-by-model grid columns — sortable + CSV-exportable via the shared <DataGrid/> (F17).
const COST_COLUMNS: Column<CostRow>[] = [
  { key: 'key', header: 'Model', accessor: (r) => r.key, sortable: true },
  { key: 'gpuHours', header: 'GPU-hours', accessor: (r) => r.gpuHours, sortable: true },
  { key: 'runs', header: 'Runs', accessor: (r) => r.runs, sortable: true },
  { key: 'costUsd', header: 'Cost', accessor: (r) => r.costUsd, render: (r) => usd(r.costUsd), sortable: true },
]

/**
 * One budget row with an in-context admin editor. The edit reuses the SAME shared write path as the
 * Projects console + the CLI (`useUpdateProject` → `PUT /projects/{name}` → `examlops` `set_project_budget`),
 * so the FinOps console gains edit parity without a second write endpoint or any risk of drift.
 */
function BudgetItem({ b, admin }: { b: BudgetRow; admin: boolean }) {
  const qc = useQueryClient()
  const update = useUpdateProject(b.project)
  const [editing, setEditing] = useState(false)
  const [gpu, setGpu] = useState(b.gpuHoursBudget != null ? String(b.gpuHoursBudget) : '')
  const [cost, setCost] = useState(b.costBudget != null ? String(b.costBudget) : '')
  const [err, setErr] = useState<string | null>(null)

  const save = async () => {
    setErr(null)
    const body: UpdateProjectBody = {}
    if (gpu.trim() !== '') body.gpuHoursBudget = Number(gpu)
    if (cost.trim() !== '') body.costBudget = Number(cost)
    try {
      await update.mutateAsync(body)
      qc.invalidateQueries({ queryKey: ['finops'] }) // refresh this console's aggregate
      setEditing(false)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Failed to update budget')
    }
  }

  return (
    <div className="rounded-lg border border-border px-4 py-2 text-sm">
      <div className="flex items-center justify-between">
        <span className="font-medium">{b.project}</span>
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span>GPU-h {budgetPct(b.gpuHoursRatio)}</span>
          <span>Cost {budgetPct(b.costRatio)}</span>
          <StatusPill status={b.overBudget ? 'critical' : 'ok'} label={b.overBudget ? 'Over budget' : 'Within'} />
          {admin && !editing && (
            <button
              onClick={() => setEditing(true)}
              aria-label={`Edit budget for ${b.project}`}
              className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-0.5 hover:bg-muted"
            >
              <Pencil className="size-3" aria-hidden="true" /> Edit
            </button>
          )}
        </div>
      </div>
      {admin && editing && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <label className="text-xs text-muted-foreground">
            GPU-h
            <input
              type="number"
              value={gpu}
              onChange={(e) => setGpu(e.target.value)}
              aria-label={`GPU-hour budget for ${b.project}`}
              className="ml-1 w-24 rounded-md border border-border bg-transparent px-2 py-1 text-xs"
            />
          </label>
          <label className="text-xs text-muted-foreground">
            Cost $
            <input
              type="number"
              value={cost}
              onChange={(e) => setCost(e.target.value)}
              aria-label={`Cost budget for ${b.project}`}
              className="ml-1 w-24 rounded-md border border-border bg-transparent px-2 py-1 text-xs"
            />
          </label>
          <button
            onClick={save}
            disabled={update.isPending}
            className="rounded-md border border-primary bg-primary px-2 py-1 text-xs text-primary-foreground disabled:opacity-50"
          >
            Save
          </button>
          <button
            onClick={() => {
              setEditing(false)
              setErr(null)
            }}
            className="rounded-md border border-border px-2 py-1 text-xs"
          >
            Cancel
          </button>
          {err && <span className="text-xs" style={{ color: 'var(--error-text)' }}>{err}</span>}
        </div>
      )}
    </div>
  )
}

export function Budgets({ budgets }: { budgets: BudgetRow[] }) {
  const admin = isAdmin()
  if (budgets.length === 0) {
    return (
      <EmptyState
        title="No budgets set"
        description={
          admin
            ? 'Create a per-project budget from its page under Platform → Projects.'
            : 'Ask an admin to set a project budget.'
        }
      />
    )
  }
  return (
    <div className="space-y-2">
      {budgets.map((b) => (
        <BudgetItem key={b.project} b={b} admin={admin} />
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
