import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './api'
import { isTerminal, type CliCommand, type CliRunDetail, type CliRunSummary, type FormValues, type Tier } from './cli'

// Resource Manager (ADR 0119). Resources come from `examlops.cli.resources` inside the CLI
// catalog: a list command, a row key, and create / show / edit / delete / actions commands with
// their row bindings resolved. Every action is one `exa` run through the CLI Console endpoints,
// so tiers, validation, containment and audit are exactly the console's. `rowsOf`,
// `rowValue` and `prefillFromRow` mirror the Python helpers of the same names.

export interface CliResource {
  id: string
  title: string
  description: string
  panel: string
  list: string
  key: string
  rows_key: string | null
  create: string | null
  show: string | null
  edit: string[]
  delete: string | null
  actions: string[]
  /** command → {param: row field}. */
  bindings: Record<string, Record<string, string>>
  columns: string[]
  tiers: Record<string, Tier>
}

export type Row = Record<string, unknown>

const norm = (s: string) => s.trim().toLowerCase().replace(/-/g, '_')

/** The item rows in a list command's JSON output, whatever shape the command prints. */
export function rowsOf(parsed: unknown, resource: Pick<CliResource, 'key' | 'rows_key'>): Row[] {
  let data: unknown = parsed
  if (data && typeof data === 'object' && !Array.isArray(data)) {
    const obj = data as Record<string, unknown>
    if (resource.rows_key) data = obj[resource.rows_key] ?? []
    else {
      const arrays = Object.values(obj).filter(Array.isArray)
      data = arrays.length === 1 ? arrays[0] : []
    }
  }
  if (!Array.isArray(data)) return []
  return data.map((r) => (r && typeof r === 'object' && !Array.isArray(r) ? (r as Row) : { [resource.key]: r }))
}

/** A row field, case- and dash-insensitively (`Name` vs `name`). */
export function rowValue(row: Row, field: string): unknown {
  const wanted = norm(field)
  for (const [k, v] of Object.entries(row)) if (norm(k) === wanted) return v
  return undefined
}

const scalar = (v: unknown): v is string | number | boolean =>
  typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean'

/** Whether a row value is usable as `param` (mirrors `fits`): no display string in a number field. */
export function fits(param: CliCommand['params'][number], v: unknown): boolean {
  if (!scalar(v)) return false
  const text = String(v).trim()
  if (param.type === 'int') return typeof v !== 'boolean' && /^-?\d+$/.test(text)
  if (param.type === 'float') return typeof v !== 'boolean' && text !== '' && Number.isFinite(Number(text))
  if (param.type === 'choice') return (param.choices ?? []).includes(text)
  return true
}

/**
 * Form values for running `command` on `row`: the resolved bindings first, then every other
 * (non-flag) parameter named like a scalar row field — a connection's `project`, an SLO's `model`.
 */
export function prefillFromRow(resource: CliResource, command: CliCommand, row: Row): FormValues {
  const values: FormValues = {}
  const specs = new Map(command.params.map((p) => [p.name, p]))
  for (const [param, field] of Object.entries(resource.bindings[command.path] ?? {})) {
    const v = rowValue(row, field)
    const spec = specs.get(param)
    if (spec && fits(spec, v)) values[param] = String(v)
  }
  for (const p of command.params) {
    if (p.name in values || p.blocked || p.implied || p.flag) continue
    const v = rowValue(row, p.name)
    if (fits(p, v)) values[p.name] = String(v)
  }
  return values
}

/** Params bound from the row's identity — shown read-only so an action cannot drift to another item. */
export function lockedParams(resource: CliResource, command: CliCommand): string[] {
  return Object.keys(resource.bindings[command.path] ?? {})
}

/** The table's columns: the declared ones present in the data first, then the rest, capped. */
export function visibleColumns(resource: CliResource, rows: Row[], max = 8): string[] {
  const seen: string[] = []
  for (const r of rows) for (const k of Object.keys(r)) if (!seen.includes(k)) seen.push(k)
  const declared = resource.columns.filter((c) => seen.some((s) => norm(s) === norm(c)))
  const keyCol = seen.find((s) => norm(s) === norm(resource.key))
  const ordered = [
    ...(keyCol && !declared.some((c) => norm(c) === norm(keyCol)) ? [keyCol] : []),
    ...declared.map((c) => seen.find((s) => norm(s) === norm(c))!),
    ...seen.filter((s) => !declared.some((c) => norm(c) === norm(s)) && s !== keyCol),
  ]
  return ordered.slice(0, max)
}

/** Run one command and wait for it to finish (list reads). Throws the CLI's own error message. */
export async function runToCompletion(
  command: string,
  args: Record<string, unknown>,
  {
    intervalMs = 400,
    timeoutMs = 120_000,
    context = '',
    confirm = '',
  }: { intervalMs?: number; timeoutMs?: number; context?: string; confirm?: string } = {},
): Promise<CliRunDetail> {
  const started = await apiFetch<CliRunSummary>('/api/v1/cli/runs', {
    method: 'POST',
    body: JSON.stringify({ command, args, format: 'json', ...(context ? { context } : {}), ...(confirm ? { confirm } : {}) }),
  })
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const run = await apiFetch<CliRunDetail>(`/api/v1/cli/runs/${encodeURIComponent(started.id)}`)
    if (isTerminal(run.status)) {
      if (run.status !== 'succeeded') throw new Error(runError(run))
      return run
    }
    if (Date.now() > deadline) throw new Error(`exa ${command} is still running — see the CLI Console history`)
    await new Promise((r) => setTimeout(r, intervalMs))
  }
}

/** The most useful one-line explanation of a failed run. */
export function runError(run: Pick<CliRunDetail, 'parsed' | 'stderr' | 'error' | 'status'>): string {
  const p = run.parsed as Record<string, unknown> | null
  if (p && typeof p === 'object' && typeof p.error === 'string') {
    return typeof p.hint === 'string' ? `${p.error} — ${p.hint}` : p.error
  }
  const tail = run.stderr.trim().split('\n').filter(Boolean).pop()
  return run.error ?? tail ?? `the command ${run.status}`
}

export const useResourceRows = (resource: CliResource | null, filters: Record<string, unknown>, enabled: boolean) =>
  useQuery({
    queryKey: ['resource', resource?.id, filters],
    queryFn: async () => rowsOf((await runToCompletion(resource!.list, filters)).parsed, resource!),
    enabled: !!resource && enabled,
    staleTime: 15_000,
    retry: false,
  })

/** Resources grouped by CLI panel, in the catalog's panel order. */
export function groupResources(resources: CliResource[], panels: string[]): [string, CliResource[]][] {
  const by = new Map<string, CliResource[]>()
  for (const r of resources) by.set(r.panel, [...(by.get(r.panel) ?? []), r])
  const order = [...panels, ...[...by.keys()].filter((p) => !panels.includes(p))]
  return order.filter((p) => by.has(p)).map((p) => [p, by.get(p)!])
}

export const RESOURCES_PATH = '/platform/resources'
export const resourceHref = (id: string) => `${RESOURCES_PATH}?r=${encodeURIComponent(id)}`
