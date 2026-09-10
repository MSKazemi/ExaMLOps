import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { Eye, MoreHorizontal, Pencil, Plus, RefreshCw, Search, SquareTerminal, Trash2 } from 'lucide-react'
import { Dialog } from '@/components/ui/dialog'
import { EmptyState } from '@/components/ui/empty-state'
import { Skeleton } from '@/components/ui/skeleton'
import { CAP, useCapabilities } from '@/lib/capabilities'
import {
  cell,
  cliCommandHref,
  defaultValues,
  missingRequired,
  toArgs,
  useCliRun,
  type CliCommand,
  type FormValues,
  type Tier,
} from '@/lib/cli'
import {
  lockedParams,
  prefillFromRow,
  rowValue,
  useResourceRows,
  visibleColumns,
  type CliResource,
  type Row,
} from '@/lib/resources'
import { cn } from '@/lib/utils'
import { CommandRunner } from './CommandRunner'
import { RunOutput } from './RunOutput'

type Role = 'create' | 'show' | 'edit' | 'delete' | 'action'

interface Pending {
  command: CliCommand
  role: Role
  row: Row | null
}

const humanize = (path: string) => {
  const verb = path.split(' ').pop() ?? path
  return verb.charAt(0).toUpperCase() + verb.slice(1).replace(/-/g, ' ')
}

const TITLE: Record<Role, (r: CliResource, c: CliCommand) => string> = {
  create: (r) => `New — ${r.title}`,
  show: () => 'Details',
  edit: (_r, c) => `Edit — ${humanize(c.path)}`,
  delete: () => 'Delete',
  action: (_r, c) => humanize(c.path),
}

/**
 * One resource as an enterprise table: filters from its list command, a New button, and per-row
 * View / Edit / Delete plus a menu of every other action — each a real `exa` command, pre-filled
 * from the row, run and audited through the CLI Console. A write refreshes the table.
 */
export function ResourceView({
  resource,
  commands,
  workspaceFiles,
}: {
  resource: CliResource
  commands: Record<string, CliCommand>
  workspaceFiles: string[]
}) {
  const caps = useCapabilities()
  const qc = useQueryClient()
  const listCmd = commands[resource.list]
  const filterParams = listCmd.params.filter((p) => !p.blocked && !p.implied)
  const [draft, setDraft] = useState<FormValues>(() => defaultValues(listCmd))
  const [applied, setApplied] = useState<FormValues>(() => defaultValues(listCmd))
  const [query, setQuery] = useState('')
  const [pending, setPending] = useState<Pending | null>(null)

  const missing = missingRequired(listCmd, applied)
  const args = useMemo(() => toArgs(listCmd, applied), [listCmd, applied])
  const { data: rows, isLoading, isFetching, error, refetch } = useResourceRows(resource, args, missing.length === 0)

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!rows || !q) return rows ?? []
    return rows.filter((r) => Object.values(r).some((v) => cell(v).toLowerCase().includes(q)))
  }, [rows, query])
  const columns = visibleColumns(resource, filtered)

  const allowed = (path: string) => {
    const tier: Tier = resource.tiers[path] ?? 'admin'
    return tier === 'read' ? caps.can(CAP.CLI_RUN) : caps.can(CAP.CLI_WRITE)
  }
  const open = (path: string | null, role: Role, row: Row | null) => {
    if (path && commands[path]) setPending({ command: commands[path], role, row })
  }
  const refresh = () => qc.invalidateQueries({ queryKey: ['resource', resource.id] })

  return (
    <section aria-label={resource.title} className="space-y-3">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-0.5">
          <h2 className="text-lg font-semibold">{resource.title}</h2>
          <p className="text-xs text-muted-foreground">{resource.description}</p>
          <p className="text-[11px] text-muted-foreground">
            Listed by{' '}
            <Link to={cliCommandHref(resource.list)} className="font-mono text-primary underline-offset-2 hover:underline">
              exa {resource.list}
            </Link>
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => refetch()}
            aria-label={`Refresh ${resource.title}`}
            className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-muted"
          >
            <RefreshCw className={cn('size-3', isFetching && 'animate-spin')} aria-hidden="true" /> Refresh
          </button>
          {resource.create && (
            <button
              type="button"
              disabled={!allowed(resource.create)}
              title={allowed(resource.create) ? undefined : 'Requires the admin role.'}
              onClick={() => open(resource.create, 'create', null)}
              className="inline-flex items-center gap-1 rounded-md border border-primary bg-primary px-2.5 py-1 text-xs text-primary-foreground disabled:opacity-50"
            >
              <Plus className="size-3" aria-hidden="true" /> New
            </button>
          )}
        </div>
      </header>

      <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            setApplied(draft)
          }}
        >
          <label className="relative text-xs">
            <span className="sr-only">Search rows</span>
            <Search className="pointer-events-none absolute left-2 top-2 size-3.5 text-muted-foreground" aria-hidden="true" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search rows…"
              aria-label={`Search ${resource.title}`}
              className="w-48 rounded-md border border-border bg-transparent py-1.5 pl-7 pr-2"
            />
          </label>
          {filterParams.map((p) => (
            <label key={p.name} className="text-xs text-muted-foreground">
              {p.kind === 'argument' ? p.name.toUpperCase() : (p.opts?.find((o) => o.startsWith('--')) ?? p.name)}
              {p.required && ' *'}
              {p.flag ? (
                <input
                  type="checkbox"
                  checked={draft[p.name] === true}
                  onChange={(e) => setDraft((d) => ({ ...d, [p.name]: e.target.checked }))}
                  className="ml-1 align-middle"
                />
              ) : p.type === 'choice' ? (
                <select
                  value={(draft[p.name] as string) ?? ''}
                  onChange={(e) => setDraft((d) => ({ ...d, [p.name]: e.target.value }))}
                  className="mt-1 block rounded-md border border-border bg-transparent px-2 py-1"
                >
                  <option value="">(any)</option>
                  {p.choices?.map((c) => (
                    <option key={c}>{c}</option>
                  ))}
                </select>
              ) : (
                <input
                  value={(draft[p.name] as string) ?? ''}
                  onChange={(e) => setDraft((d) => ({ ...d, [p.name]: e.target.value }))}
                  placeholder={p.help}
                  className="mt-1 block w-40 rounded-md border border-border bg-transparent px-2 py-1"
                />
              )}
            </label>
          ))}
          {filterParams.length > 0 && (
            <button type="submit" className="rounded-md border border-border px-2.5 py-1 text-xs hover:bg-muted">
              Apply
            </button>
          )}
        </form>

      {missing.length > 0 && (
        <EmptyState title={`Enter ${missing.join(', ')} to list ${resource.title.toLowerCase()}`} description={`exa ${resource.list} needs it.`} />
      )}
      {missing.length === 0 && isLoading && <Skeleton className="h-40 w-full" />}
      {error && (
        <div role="alert" className="rounded-lg border border-border p-3 text-xs">
          <p className="font-medium">Couldn&rsquo;t list {resource.title.toLowerCase()}</p>
          <p className="mt-1 text-muted-foreground">{error instanceof Error ? error.message : String(error)}</p>
          <Link to={cliCommandHref(resource.list)} className="mt-1 inline-flex items-center gap-1 text-primary hover:underline">
            <SquareTerminal className="size-3" aria-hidden="true" /> Open in the CLI Console
          </Link>
        </div>
      )}
      {rows && !error && filtered.length === 0 && (
        <EmptyState
          title={rows.length ? 'No rows match the search' : `No ${resource.title.toLowerCase()} yet`}
          description={resource.create && !rows.length ? 'Create the first one with New.' : undefined}
        />
      )}

      {filtered.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-xs">
            <thead className="bg-muted/40">
              <tr>
                {columns.map((c) => (
                  <th key={c} scope="col" className="px-2 py-1.5 text-left font-semibold">
                    {c}
                  </th>
                ))}
                <th scope="col" className="px-2 py-1.5 text-right font-semibold">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row, i) => (
                <tr key={`${cell(rowValue(row, resource.key))}-${i}`} className="border-t border-border hover:bg-muted/30">
                  {columns.map((c) => (
                    <td key={c} className="max-w-[18rem] truncate px-2 py-1.5 align-top" title={cell(row[c])}>
                      {cell(row[c])}
                    </td>
                  ))}
                  <td className="whitespace-nowrap px-2 py-1 text-right">
                    <RowActions resource={resource} row={row} allowed={allowed} onOpen={open} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="border-t border-border px-2 py-1 text-[10px] text-muted-foreground">
            {filtered.length} of {rows?.length ?? 0} row(s)
          </p>
        </div>
      )}

      {pending && (
        <ActionDialog
          resource={resource}
          pending={pending}
          workspaceFiles={workspaceFiles}
          onClose={() => setPending(null)}
          onChanged={refresh}
        />
      )}
    </section>
  )
}

function RowActions({
  resource,
  row,
  allowed,
  onOpen,
}: {
  resource: CliResource
  row: Row
  allowed: (path: string) => boolean
  onOpen: (path: string | null, role: Role, row: Row | null) => void
}) {
  // The menu is positioned against the viewport (from the trigger's rect): the table sits in a
  // horizontally scrolling box, which would clip an absolutely positioned menu to its own height.
  const [menu, setMenu] = useState<{ top: number; right: number } | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const label = cell(rowValue(row, resource.key))
  useEffect(() => {
    if (!menu) return
    const close = (e: Event) => {
      if (e instanceof KeyboardEvent ? e.key === 'Escape' : e.type !== 'mousedown' || !menuRef.current?.contains(e.target as Node)) setMenu(null)
    }
    window.addEventListener('mousedown', close)
    window.addEventListener('keydown', close)
    window.addEventListener('resize', close)
    window.addEventListener('scroll', close, true)
    return () => {
      window.removeEventListener('mousedown', close)
      window.removeEventListener('keydown', close)
      window.removeEventListener('resize', close)
      window.removeEventListener('scroll', close, true)
    }
  }, [menu])
  const toggleMenu = (e: React.MouseEvent<HTMLButtonElement>) => {
    if (menu) return setMenu(null)
    const rect = e.currentTarget.getBoundingClientRect()
    setMenu({ top: rect.bottom + 4, right: Math.max(8, window.innerWidth - rect.right) })
  }

  const extraEdits = resource.edit.slice(1)
  const more = [...extraEdits.map((p) => [p, 'edit'] as const), ...resource.actions.map((p) => [p, 'action'] as const)]
  const iconBtn = 'inline-flex items-center rounded p-1 hover:bg-muted disabled:opacity-40'

  return (
    <div ref={menuRef} className="relative inline-flex items-center gap-0.5">
      {resource.show && (
        <button type="button" className={iconBtn} disabled={!allowed(resource.show)} aria-label={`View ${label}`} title="View" onClick={() => onOpen(resource.show, 'show', row)}>
          <Eye className="size-3.5" aria-hidden="true" />
        </button>
      )}
      {resource.edit[0] && (
        <button type="button" className={iconBtn} disabled={!allowed(resource.edit[0])} aria-label={`Edit ${label}`} title={allowed(resource.edit[0]) ? 'Edit' : 'Requires the admin role.'} onClick={() => onOpen(resource.edit[0], 'edit', row)}>
          <Pencil className="size-3.5" aria-hidden="true" />
        </button>
      )}
      {resource.delete && (
        <button type="button" className={cn(iconBtn, 'text-[color:var(--error-text)]')} disabled={!allowed(resource.delete)} aria-label={`Delete ${label}`} title={allowed(resource.delete) ? 'Delete' : 'Requires the admin role.'} onClick={() => onOpen(resource.delete, 'delete', row)}>
          <Trash2 className="size-3.5" aria-hidden="true" />
        </button>
      )}
      {more.length > 0 && (
        <>
          <button type="button" className={iconBtn} aria-haspopup="menu" aria-expanded={!!menu} aria-label={`More actions for ${label}`} onClick={toggleMenu}>
            <MoreHorizontal className="size-3.5" aria-hidden="true" />
          </button>
          {menu && (
            <div
              role="menu"
              style={{ position: 'fixed', top: menu.top, right: menu.right }}
              className="z-40 max-h-[60vh] min-w-44 overflow-y-auto rounded-md border border-border bg-background py-1 text-left shadow-lg"
            >
              {more.map(([path, role]) => (
                <button
                  key={path}
                  type="button"
                  role="menuitem"
                  disabled={!allowed(path)}
                  title={allowed(path) ? `exa ${path}` : 'Requires the admin role.'}
                  onClick={() => {
                    setMenu(null)
                    onOpen(path, role, row)
                  }}
                  className="block w-full px-3 py-1.5 text-left text-xs hover:bg-muted disabled:opacity-40"
                >
                  {humanize(path)}
                  {resource.tiers[path] === 'destructive' && <span className="ml-1 text-[10px] text-[color:var(--error-text)]">destructive</span>}
                </button>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}

function ActionDialog({
  resource,
  pending,
  workspaceFiles,
  onClose,
  onChanged,
}: {
  resource: CliResource
  pending: Pending
  workspaceFiles: string[]
  onClose: () => void
  onChanged: () => void
}) {
  const { command, role, row } = pending
  const caps = useCapabilities()
  const [runId, setRunId] = useState<string | null>(null)
  const { data: run } = useCliRun(runId)
  const notified = useRef(false)
  const writes = (resource.tiers[command.path] ?? 'admin') !== 'read'

  // A successful write changes what the table shows: refresh it once, and keep the dialog open
  // on the output so the operator sees what happened.
  useEffect(() => {
    if (writes && run?.status === 'succeeded' && !notified.current) {
      notified.current = true
      onChanged()
    }
  }, [run?.status, writes, onChanged])

  const initialValues = row ? prefillFromRow(resource, command, row) : {}
  const locked = row ? lockedParams(resource, command) : []
  const itemLabel = row ? cell(rowValue(row, resource.key)) : ''

  return (
    <Dialog
      title={
        <>
          {TITLE[role](resource, command)}
          {itemLabel && <span className="font-mono font-normal text-muted-foreground"> · {itemLabel}</span>}
        </>
      }
      subtitle={<code>exa {command.path}</code>}
      onClose={onClose}
      footer={
        <button type="button" onClick={onClose} className="rounded-md border border-border px-3 py-1 text-xs hover:bg-muted">
          {run && run.status === 'succeeded' ? 'Done' : 'Close'}
        </button>
      }
    >
      <div className="space-y-4">
        <CommandRunner
          command={command}
          workspaceFiles={workspaceFiles}
          onStarted={setRunId}
          initialValues={initialValues}
          locked={locked}
          compact
          runLabel={role === 'create' ? 'Create' : role === 'delete' ? 'Delete' : role === 'edit' ? 'Save' : role === 'show' ? 'Reload' : 'Run'}
          autoRun={role === 'show'}
        />
        {runId && <RunOutput runId={runId} canDownload={caps.can(CAP.CLI_WRITE)} />}
      </div>
    </Dialog>
  )
}
