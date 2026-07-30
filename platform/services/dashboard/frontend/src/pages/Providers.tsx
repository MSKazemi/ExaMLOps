import { useState } from 'react'
import { Boxes, CheckCircle2, PlusCircle, Trash2 } from 'lucide-react'
import type { Column } from '@/lib/datagrid'
import { ConsoleView, type RowAction } from '@/components/console'
import { CAP, useCapabilities } from '@/lib/capabilities'
import { useProviders, useActivateProvider, useDeleteProvider, type ProviderRow } from '@/lib/providers'
import { ProviderEditor } from '@/components/ProvidersCard'

/**
 * Providers console (ADR 0074, dashboard-enterprise-rebuild) — the Platform-group surface over the
 * `exa providers` capability. Lists a project's authored calculation providers (FinOps cost/carbon,
 * drift, promotion, …) and lets an admin activate or delete one. Every mutation flows through the
 * shared `examlops.providers` code path (via `lib/providers` → the `/api/v1/providers` router), is
 * `providers.manage`-gated (viewer read-only, shown-but-disabled with the deny reason), and audited
 * `source=dashboard`. Authoring/uploading Python uses the same AST-sandboxed editor as the
 * per-project ProvidersCard.
 */
export function Providers() {
  const { can } = useCapabilities()
  const [project, setProject] = useState('default')
  const [showNew, setShowNew] = useState(false)
  const { data, isLoading, error } = useProviders(project)
  const activate = useActivateProvider(project)
  const del = useDeleteProvider(project)
  const canManage = can(CAP.PROVIDERS_MANAGE)

  const columns: Column<ProviderRow>[] = [
    {
      key: 'project',
      header: 'Project',
      accessor: () => project,
      render: () => <span className="font-mono text-xs text-muted-foreground">{project}</span>,
    },
    {
      key: 'domain',
      header: 'Domain',
      accessor: (r) => r.domain,
      sortable: true,
      facet: true,
      render: (r) => (
        <span
          className="text-[11px] px-1.5 py-0.5 rounded font-mono"
          style={{ background: 'var(--surface-2)', color: 'var(--subtle-text)' }}
        >
          {r.domain}
        </span>
      ),
    },
    {
      key: 'name',
      header: 'Name',
      accessor: (r) => r.name,
      sortable: true,
      render: (r) => <span className="font-mono text-sm">{r.name}</span>,
    },
    {
      key: 'active',
      header: 'Active',
      accessor: (r) => (r.active ? 'active' : ''),
      facet: true,
      render: (r) =>
        r.active ? (
          <span
            className="text-[10px] px-1.5 py-0.5 rounded-full uppercase tracking-wide"
            style={{ background: 'oklch(0.72 0.18 155 / 12%)', border: '1px solid oklch(0.72 0.18 155 / 30%)', color: 'var(--success-text)' }}
          >
            active
          </span>
        ) : (
          <span className="text-muted-foreground text-xs">—</span>
        ),
    },
    {
      key: 'status',
      header: 'Gate / Trust',
      accessor: (r) => (r.ok ? 'ok' : 'error'),
      facet: true,
      render: (r) =>
        r.ok ? (
          <span
            className="text-[10px] px-1.5 py-0.5 rounded-full uppercase tracking-wide"
            style={{ background: 'oklch(0.72 0.18 155 / 10%)', border: '1px solid oklch(0.72 0.18 155 / 25%)', color: 'var(--success-text)' }}
          >
            passed
          </span>
        ) : (
          <span
            className="text-[10px] px-1.5 py-0.5 rounded-full"
            style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 28%)', color: 'var(--error-text)' }}
            title={r.error ?? ''}
          >
            gate error
          </span>
        ),
    },
  ]

  const rowActions: RowAction<ProviderRow>[] = [
    {
      id: 'activate',
      label: 'Activate',
      icon: CheckCircle2,
      variant: 'success',
      capability: CAP.PROVIDERS_MANAGE,
      visible: (r) => !r.active,
      run: async (r) => {
        await activate.mutateAsync({ domain: r.domain, name: r.name })
      },
    },
    {
      id: 'delete',
      label: 'Delete',
      icon: Trash2,
      variant: 'danger',
      capability: CAP.PROVIDERS_MANAGE,
      confirm: true,
      run: async (r) => {
        await del.mutateAsync({ domain: r.domain, name: r.name })
      },
    },
  ]

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <ConsoleView<ProviderRow>
        title="Providers"
        subtitle="Authored calculation providers (FinOps cost/carbon, drift, promotion, …) for a project. Uploads are AST-sandboxed and mirror the exa providers CLI."
        icon={Boxes}
        columns={columns}
        rows={data}
        getRowId={(r) => `${r.domain}/${r.name}`}
        rowActions={rowActions}
        isLoading={isLoading}
        error={error}
        errorMessage="Failed to load providers. Is the platform database reachable?"
        emptyTitle="No authored providers"
        emptyDescription={
          canManage
            ? 'Click "New provider" to author one for this project.'
            : 'An admin can author calculation providers for this project.'
        }
        storageKey="providers"
        toolbar={
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 text-sm text-muted-foreground">
              Project
              <input
                aria-label="Provider project"
                value={project}
                onChange={(e) => setProject(e.target.value)}
                className="w-40 rounded-md border border-border bg-transparent px-2 py-1 text-xs"
              />
            </label>
            <button
              type="button"
              onClick={() => setShowNew(true)}
              disabled={!canManage}
              aria-label="New provider"
              title={canManage ? undefined : 'Requires the admin role.'}
              className="inline-flex items-center gap-1.5 rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50"
            >
              <PlusCircle className="w-3 h-3" aria-hidden="true" /> New provider
            </button>
          </div>
        }
      />

      {showNew && canManage && (
        <ProviderEditor project={project} onClose={() => setShowNew(false)} />
      )}
    </div>
  )
}
