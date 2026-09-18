import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'
import { getToken } from './auth'
import { ApiError, parseProblem } from './errors'
import { fuzzyScore } from './search'

// CLI Console (ADR 0119). The catalog is the live `exa` command tree as the platform's
// `examlops.cli.surface` describes it; runs execute the real CLI server-side. Everything the UI
// decides here (badges, disabled buttons, the escalation preview) is an affordance — the BFF
// re-validates every argument and re-derives every permission.

export type Tier = 'read' | 'admin' | 'destructive' | 'cli_only'

export interface CliParam {
  name: string
  kind: 'argument' | 'option'
  type: 'string' | 'int' | 'float' | 'bool' | 'choice'
  required?: boolean
  multiple?: boolean
  nargs?: number
  flag?: boolean
  opts?: string[]
  secondary_opts?: string[]
  help?: string
  default?: unknown
  choices?: string[]
  min?: number
  max?: number
  path?: boolean
  path_or_url?: boolean
  network?: boolean
  secret?: boolean
  persisting?: boolean
  blocked?: boolean
  /** The command's own `--yes`: always passed (the console's confirmation replaces it), never shown. */
  implied?: boolean
}

export interface CliExample {
  cmd: string
  comment: string
}

export interface CliCommand {
  path: string
  group: string
  panel: string
  help: string
  short_help: string
  examples: CliExample[]
  tier: Tier
  reason: string | null
  params: CliParam[]
  forced_args: string[]
}

export interface CliCatalog {
  commands: CliCommand[]
  total: number
  tiers: Record<Tier, number>
  unclassified: string[]
  panels: string[]
  /** The CLI's verbs composed into manageable objects (see `lib/resources.ts`). */
  resources?: import('./resources').CliResource[]
  resource_coverage?: { resources: number; commands: number; runnable: number }
}

export type RunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'timeout' | 'cancelled' | 'error'

export interface CliRunSummary {
  id: string
  command: string
  display: string
  tier: Tier
  format: string
  actor: string
  status: RunStatus
  exit_code: number | null
  created_at: number
  started_at: number | null
  finished_at: number | null
  duration_ms: number | null
  error: string | null
}

export interface CliRunDetail extends CliRunSummary {
  args: Record<string, unknown>
  stdout: string
  stderr: string
  truncated: boolean
  parsed: unknown
  files: string[]
}

export interface StartRunBody {
  command: string
  args: Record<string, unknown>
  format: 'json' | 'text'
  context?: string
  confirm?: string
}

export interface WorkspaceFile {
  path: string
  size: number
  modified: number
}

export type FormValues = Record<string, string | boolean | string[] | undefined>

const TERMINAL: ReadonlySet<RunStatus> = new Set(['succeeded', 'failed', 'timeout', 'cancelled', 'error'])

export const isTerminal = (status: RunStatus | undefined): boolean => !!status && TERMINAL.has(status)

// ── queries ──────────────────────────────────────────────────────────────────────────────

export const useCliCatalog = (enabled = true) =>
  useQuery<CliCatalog>({
    queryKey: ['cli', 'catalog'],
    queryFn: () => apiFetch<CliCatalog>('/api/v1/cli/catalog'),
    staleTime: 5 * 60_000,
    enabled,
  })

export const useCliRuns = () =>
  useQuery<{ runs: CliRunSummary[] }>({
    queryKey: ['cli', 'runs'],
    queryFn: () => apiFetch<{ runs: CliRunSummary[] }>('/api/v1/cli/runs'),
    refetchInterval: 5_000,
  })

/** One run, polled until it reaches a terminal state. */
export const useCliRun = (id: string | null) =>
  useQuery<CliRunDetail>({
    queryKey: ['cli', 'run', id],
    queryFn: () => apiFetch<CliRunDetail>(`/api/v1/cli/runs/${encodeURIComponent(id ?? '')}`),
    enabled: !!id,
    refetchInterval: (q) => (isTerminal(q.state.data?.status) ? false : 700),
  })

export const useStartCliRun = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: StartRunBody) =>
      apiFetch<CliRunSummary>('/api/v1/cli/runs', { method: 'POST', body: JSON.stringify(body) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['cli', 'runs'] }),
  })
}

export const useCancelCliRun = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<{ cancelled: boolean }>(`/api/v1/cli/runs/${encodeURIComponent(id)}/cancel`, {
        method: 'POST',
      }),
    onSuccess: (_d, id) => {
      qc.invalidateQueries({ queryKey: ['cli', 'run', id] })
      qc.invalidateQueries({ queryKey: ['cli', 'runs'] })
    },
  })
}

export const useWorkspace = (enabled: boolean) =>
  useQuery<{ files: WorkspaceFile[] }>({
    queryKey: ['cli', 'workspace'],
    queryFn: () => apiFetch<{ files: WorkspaceFile[] }>('/api/v1/cli/workspace'),
    enabled,
  })

/** Upload a file into the CLI workspace (multipart — `apiFetch` would force a JSON body). */
export async function uploadWorkspaceFile(file: File, path: string, overwrite: boolean): Promise<{ path: string }> {
  const form = new FormData()
  form.append('file', file)
  form.append('path', path || file.name)
  form.append('overwrite', String(overwrite))
  const token = getToken()
  const res = await fetch('/api/v1/cli/workspace', {
    method: 'POST',
    body: form,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!res.ok) {
    let body: unknown = null
    try {
      body = await res.json()
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, parseProblem(res.status, body))
  }
  return res.json()
}

export const deleteWorkspaceFile = (path: string) =>
  apiFetch<{ deleted: boolean }>(`/api/v1/cli/workspace/file?path=${encodeURIComponent(path)}`, {
    method: 'DELETE',
  })

/** Download a workspace file with the bearer token (a plain link would carry no auth header). */
export async function downloadWorkspaceFile(path: string): Promise<void> {
  const token = getToken()
  const res = await fetch(`/api/v1/cli/workspace/file?path=${encodeURIComponent(path)}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!res.ok) throw new ApiError(res.status, parseProblem(res.status, null))
  const url = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = url
  a.download = path.split('/').pop() || 'download'
  a.click()
  URL.revokeObjectURL(url)
}

// ── pure helpers ─────────────────────────────────────────────────────────────────────────

export const TIER_LABEL: Record<Tier, string> = {
  read: 'Read',
  admin: 'Admin',
  destructive: 'Destructive',
  cli_only: 'CLI only',
}

export const TIER_HELP: Record<Tier, string> = {
  read: 'Reads platform state. Anyone signed in can run it.',
  admin: 'Changes platform state or reads sensitive data. Requires the admin role.',
  destructive: 'Erases or overwrites state. Admin role, and you type the command to confirm.',
  cli_only: 'Cannot run from a browser — see the reason below.',
}

/** Commands grouped by their `exa --help` lifecycle panel, in the CLI's own panel order. */
export function groupByPanel(catalog: CliCatalog, commands: CliCommand[] = catalog.commands): [string, CliCommand[]][] {
  const byPanel = new Map<string, CliCommand[]>()
  for (const c of commands) byPanel.set(c.panel, [...(byPanel.get(c.panel) ?? []), c])
  const order = [...catalog.panels, ...[...byPanel.keys()].filter((p) => !catalog.panels.includes(p))]
  return order.filter((p) => byPanel.has(p)).map((p) => [p, byPanel.get(p)!])
}

/** Fuzzy search over path, help and group; empty query ⇒ everything (catalog order). */
export function searchCommands(commands: CliCommand[], query: string): CliCommand[] {
  const q = query.trim()
  if (!q) return commands
  return commands
    .map((c) => ({
      c,
      s: Math.max(fuzzyScore(q, c.path) * 1.5, fuzzyScore(q, c.short_help), fuzzyScore(q, c.group)),
    }))
    .filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s || a.c.path.localeCompare(b.c.path))
    .map((x) => x.c)
}

/** Initial form values from a command's declared defaults. */
export function defaultValues(cmd: CliCommand): FormValues {
  const out: FormValues = {}
  for (const p of cmd.params) {
    if (p.flag) out[p.name] = p.default === true
    else if (p.default !== undefined && p.default !== null && !Array.isArray(p.default)) out[p.name] = String(p.default)
  }
  return out
}

function isEmpty(v: unknown): boolean {
  return v === undefined || v === null || v === '' || (Array.isArray(v) && v.length === 0)
}

/** Split a variadic / repeatable text box into values (one per line; spaces for variadic args). */
function listValue(p: CliParam, v: string | string[]): string[] {
  if (Array.isArray(v)) return v.filter((x) => x !== '')
  if (p.kind === 'argument' && (p.nargs ?? 1) === -1) return v.split(/\s+/).filter(Boolean)
  return v.split('\n').map((x) => x.trim()).filter(Boolean)
}

/**
 * The request `args` for a form: only values that differ from the command's defaults, typed for
 * the server (booleans stay booleans; repeatable/variadic params become lists).
 */
export function toArgs(cmd: CliCommand, values: FormValues): Record<string, unknown> {
  const args: Record<string, unknown> = {}
  for (const p of cmd.params) {
    const v = values[p.name]
    if (p.blocked || p.implied) continue
    if (p.flag) {
      if (typeof v === 'boolean' && v !== (p.default === true)) args[p.name] = v
      continue
    }
    if (isEmpty(v) || typeof v === 'boolean') continue
    const many = p.multiple || (p.nargs ?? 1) !== 1
    if (many) {
      const list = listValue(p, v as string | string[])
      if (list.length) args[p.name] = list
    } else if (String(v) !== String(p.default ?? '')) {
      args[p.name] = v
    } else if (p.required) {
      args[p.name] = v
    }
  }
  return args
}

/**
 * The form values that produced a run's recorded `args` — the inverse of {@link toArgs}, for
 * "Run again". Secrets are never restored: the record holds only their mask, so a required secret
 * comes back empty and the form asks for it again. Params the command no longer has, and params
 * the console never sends (blocked, implied), are dropped rather than guessed at.
 */
export function fromArgs(cmd: CliCommand, args: Record<string, unknown>): FormValues {
  const out: FormValues = {}
  for (const p of cmd.params) {
    if (!(p.name in args) || p.secret || p.blocked || p.implied) continue
    const v = args[p.name]
    if (p.flag) out[p.name] = v === true
    // As the form asks for them: variadic arguments space-separated, repeatable options one per line.
    else if (Array.isArray(v)) out[p.name] = v.map(String).join(p.kind === 'argument' ? ' ' : '\n')
    else if (v !== null && v !== undefined) out[p.name] = String(v)
  }
  return out
}

/** Required params the form has not filled in (empty list ⇒ ready to run). */
export function missingRequired(cmd: CliCommand, values: FormValues): string[] {
  return cmd.params.filter((p) => p.required && !p.flag && isEmpty(values[p.name])).map((p) => p.name)
}

const quote = (s: string) => (/^[\w@%+=:,./-]+$/.test(s) ? s : `'${s.replace(/'/g, `'\\''`)}'`)

/** The equivalent terminal command for the current form (secrets masked). Mirrors the server. */
export function commandLine(cmd: CliCommand, args: Record<string, unknown>, format: 'json' | 'text'): string {
  const parts = ['exa', ...(format === 'json' ? ['--json'] : []), ...cmd.path.split(' ')]
  const positionals: string[] = []
  for (const p of cmd.params) {
    if (p.implied) {
      const long = p.opts?.find((o) => o.startsWith('--')) ?? p.opts?.[0]
      if (long) parts.push(long)
      continue
    }
    if (!(p.name in args)) continue
    const raw = args[p.name]
    const items = (Array.isArray(raw) ? raw : [raw]).map((x) => (p.secret ? '***' : String(x)))
    if (p.flag) {
      const opts = raw ? p.opts : p.secondary_opts
      const long = opts?.find((o) => o.startsWith('--')) ?? opts?.[0]
      if (long) parts.push(long)
    } else if (p.kind === 'argument') {
      positionals.push(...items)
    } else {
      const long = p.opts?.find((o) => o.startsWith('--')) ?? p.opts?.[0] ?? `--${p.name}`
      for (const item of items) parts.push(`${long}=${item}`)
    }
  }
  parts.push(...cmd.forced_args)
  if (positionals.some((x) => x.startsWith('-'))) parts.push('--')
  parts.push(...positionals)
  return parts.map(quote).join(' ')
}

const RANK: Record<Tier, number> = { read: 0, admin: 1, destructive: 2, cli_only: 3 }

/**
 * The tier the server will apply to these args — a `read` becomes `admin` when a persisting flag
 * (including one that defaults on), a filesystem path, or a network target is supplied.
 * An affordance only: the server computes the same thing and is the one that decides.
 */
export function effectiveTier(cmd: CliCommand, args: Record<string, unknown>): Tier {
  let tier = cmd.tier
  const raise = (t: Tier) => {
    if (RANK[t] > RANK[tier]) tier = t
  }
  for (const p of cmd.params) {
    const supplied = p.name in args
    if (p.flag && p.persisting) {
      const effective = supplied ? args[p.name] === true : p.default === true
      if (effective) raise('admin')
      continue
    }
    if (!supplied) continue
    if (p.path || p.network || p.persisting) raise('admin')
  }
  return tier
}

export type OutputShape = 'table' | 'record' | 'json' | 'text' | 'empty'

/** How to render a run's parsed output. */
export function outputShape(parsed: unknown, stdout: string): OutputShape {
  if (parsed === null || parsed === undefined) return stdout.trim() ? 'text' : 'empty'
  if (Array.isArray(parsed)) {
    if (parsed.length === 0) return 'empty'
    return parsed.every((r) => r && typeof r === 'object' && !Array.isArray(r)) ? 'table' : 'json'
  }
  if (typeof parsed === 'object') return 'record'
  return 'json'
}

/** Column names across a list of records, in first-seen order. */
export function columnsOf(rows: Record<string, unknown>[]): string[] {
  const cols: string[] = []
  for (const r of rows) for (const k of Object.keys(r)) if (!cols.includes(k)) cols.push(k)
  return cols
}

export function cell(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

// ── console ⇄ CLI cross-links ────────────────────────────────────────────────────────────

/**
 * Which bespoke console covers a CLI command, by longest path prefix. Lets the CLI Console say
 * "also in the Drift console", and lets every console link back to its `exa` commands.
 */
export const CONSOLE_FOR_PREFIX: Record<string, string> = {
  models: '/build/models',
  modelzoo: '/build/models',
  embedding: '/build/models',
  pipeline: '/build/pipelines',
  retrain: '/build/pipelines',
  scaffold: '/build/models',
  data: '/build/datasets',
  cards: '/build/datasets',
  feature: '/build/features',
  features: '/build/features',
  assets: '/build/assets',
  prompt: '/build/prompts',
  'serve ab': '/serve/traffic',
  'serve shadow': '/serve/traffic',
  'serve traffic': '/serve/traffic',
  'serve traffic-list': '/serve/traffic',
  'serve challenger': '/serve/challenger',
  'serve autoscale': '/serve/scaling',
  'serve routing': '/serve/scaling',
  gateway: '/serve/gateway',
  genai: '/serve/llmops',
  rag: '/serve/llmops',
  vector: '/serve/llmops',
  guardrails: '/serve/llmops',
  agentops: '/serve/llmops',
  drift: '/operate/drift',
  autopilot: '/operate/autopilot',
  slo: '/operate/slos',
  admission: '/operate/admission',
  hpc: '/operate/facility',
  fleet: '/operate/facility',
  hardware: '/operate/facility',
  federated: '/operate/facility',
  finops: '/operate/finops',
  report: '/operate/finops',
  compliance: '/govern/compliance',
  governance: '/govern/governance',
  audit: '/govern/audit',
  approvals: '/govern/approvals',
  fairness: '/govern/fairness',
  secrets: '/govern/secrets',
  project: '/platform/projects',
  namespace: '/platform/projects',
  connection: '/platform/projects',
  workbench: '/platform/projects',
  events: '/platform/events',
  stack: '/platform/services',
  providers: '/platform/providers',
  config: '/platform/config',
  env: '/platform/config',
  docs: '/documents',
}

/** The bespoke console for a command path, if one covers it. */
export function consoleFor(path: string): string | null {
  const words = path.split(' ')
  for (let n = words.length; n > 0; n--) {
    const hit = CONSOLE_FOR_PREFIX[words.slice(0, n).join(' ')]
    if (hit) return hit
  }
  return null
}

/** The CLI prefixes a console covers — the `?filter=` for its "exa commands" link. */
export function cliPrefixesFor(pathname: string): string[] {
  return Object.entries(CONSOLE_FOR_PREFIX)
    .filter(([, route]) => pathname === route || pathname.startsWith(route + '/'))
    .map(([prefix]) => prefix)
}

/** Commands matching any of the given path prefixes (a group filter from a console link). */
export function filterByPrefixes(commands: CliCommand[], prefixes: string[]): CliCommand[] {
  if (!prefixes.length) return commands
  return commands.filter((c) => prefixes.some((p) => c.path === p || c.path.startsWith(p + ' ')))
}

export const CLI_CONSOLE_PATH = '/platform/cli'

export const cliCommandHref = (path: string) => `${CLI_CONSOLE_PATH}?cmd=${encodeURIComponent(path)}`
export const cliFilterHref = (prefixes: string[]) =>
  `${CLI_CONSOLE_PATH}?filter=${encodeURIComponent(prefixes.join(','))}`
