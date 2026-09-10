import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Copy, Play, ShieldAlert, Terminal } from 'lucide-react'
import { CAP, useCapabilities } from '@/lib/capabilities'
import {
  commandLine,
  consoleFor,
  defaultValues,
  effectiveTier,
  missingRequired,
  TIER_HELP,
  toArgs,
  useStartCliRun,
  type CliCommand,
  type CliParam,
  type FormValues,
} from '@/lib/cli'
import { TierBadge } from './TierBadge'

interface Props {
  command: CliCommand
  /** Workspace file names, offered as suggestions for path parameters. */
  workspaceFiles: string[]
  onStarted: (runId: string) => void
  /** Values to start from (a resource row's fields), over the command's own defaults. */
  initialValues?: FormValues
  /** Params fixed by the item being acted on — shown, but not editable. */
  locked?: string[]
  /** Inside a resource dialog: no examples, console link or tier explainer. */
  compact?: boolean
  /** Label for the run button (e.g. "Create", "Delete"). */
  runLabel?: string
  /** Run once on open when nothing is missing (a resource's View). */
  autoRun?: boolean
}

/** One command: its help, an auto-generated form for every parameter, and a Run button. */
export function CommandRunner({
  command,
  workspaceFiles,
  onStarted,
  initialValues,
  locked = [],
  compact = false,
  runLabel = 'Run',
  autoRun = false,
}: Props) {
  const caps = useCapabilities()
  const [values, setValues] = useState<FormValues>(() => ({ ...defaultValues(command), ...initialValues }))
  const [format, setFormat] = useState<'json' | 'text'>('json')
  const [context, setContext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const start = useStartCliRun()

  const args = useMemo(() => toArgs(command, values), [command, values])
  const tier = effectiveTier(command, args)
  const line = commandLine(command, args, format)
  const missing = missingRequired(command, values)
  const escalated = tier !== command.tier
  const consolePath = consoleFor(command.path)

  const permitted = tier === 'read' ? caps.can(CAP.CLI_RUN) : caps.can(CAP.CLI_WRITE)
  const needsConfirm = tier === 'destructive'
  const blockedReason =
    command.tier === 'cli_only'
      ? 'This command cannot run from the dashboard.'
      : !permitted
        ? escalated
          ? 'These arguments change platform state — requires the admin role.'
          : 'Requires the admin role.'
        : missing.length
          ? `Fill in: ${missing.join(', ')}`
          : needsConfirm && confirm.trim() !== command.path
            ? `Type "${command.path}" to confirm.`
            : null

  const set = (name: string, v: FormValues[string]) => setValues((prev) => ({ ...prev, [name]: v }))

  const run = async () => {
    setMsg(null)
    try {
      const r = await start.mutateAsync({
        command: command.path,
        args,
        format,
        ...(context.trim() ? { context: context.trim() } : {}),
        ...(needsConfirm ? { confirm: confirm.trim() } : {}),
      })
      setConfirm('')
      onStarted(r.id)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Could not start the command')
    }
  }

  // View opens loaded: run once on mount when the form is already complete and permitted.
  const autoRan = useRef(false)
  useEffect(() => {
    if (autoRun && !autoRan.current && !blockedReason) {
      autoRan.current = true
      void run()
    }
    // Only on mount — later edits wait for an explicit click.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const copy = () => {
    navigator.clipboard?.writeText(line).then(
      () => {
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      },
      () => {},
    )
  }

  // `implied` (the command's own --yes) is passed by the console itself — never a checkbox.
  const runnable = command.params.filter((p) => !p.blocked && !p.implied)
  const blocked = command.params.filter((p) => p.blocked)

  return (
    <section aria-label={`exa ${command.path}`} className="space-y-4">
      <header className={compact ? 'hidden' : 'space-y-1'}>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-mono text-lg font-semibold">exa {command.path}</h2>
          <TierBadge tier={command.tier} />
          {escalated && (
            <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
              → <TierBadge tier={tier} /> with these arguments
            </span>
          )}
        </div>
        {command.help && <p className="whitespace-pre-line text-sm text-muted-foreground">{command.help}</p>}
        <p className="text-[11px] text-muted-foreground">{TIER_HELP[command.tier]}</p>
        {consolePath && (
          <p className="text-[11px]">
            Also on a console:{' '}
            <Link to={consolePath} className="text-primary underline-offset-2 hover:underline">
              {consolePath}
            </Link>
          </p>
        )}
      </header>

      {command.tier === 'cli_only' ? (
        <div role="note" className="flex gap-2 rounded-lg border border-border p-3 text-sm">
          <Terminal className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <div className="space-y-1">
            <p className="font-medium">Runs in a terminal only</p>
            <p className="text-muted-foreground">{command.reason}</p>
          </div>
        </div>
      ) : (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault()
            if (!blockedReason) void run()
          }}
        >
          {runnable.length === 0 && <p className="text-xs text-muted-foreground">This command takes no parameters.</p>}
          <div className="grid gap-3 sm:grid-cols-2">
            {runnable.map((p) => (
              <ParamField
                key={p.name}
                param={p}
                value={values[p.name]}
                onChange={(v) => set(p.name, v)}
                locked={locked.includes(p.name)}
              />
            ))}
          </div>
          {workspaceFiles.length > 0 && runnable.some((p) => p.path) && (
            <datalist id="cli-workspace-files">
              {workspaceFiles.map((f) => (
                <option key={f} value={f} />
              ))}
            </datalist>
          )}
          {blocked.length > 0 && (
            <p className="text-[11px] text-muted-foreground">
              Not available from the dashboard:{' '}
              {blocked.map((p) => (
                <code key={p.name} className="mr-1">
                  {longOpt(p)}
                </code>
              ))}
              (endless or secret-revealing).
            </p>
          )}

          <details className="text-xs">
            <summary className="cursor-pointer text-muted-foreground">Output &amp; context</summary>
            <div className="mt-2 flex flex-wrap gap-3">
              <label className="text-muted-foreground">
                Output
                <select
                  value={format}
                  onChange={(e) => setFormat(e.target.value as 'json' | 'text')}
                  className="mt-1 block rounded-md border border-border bg-transparent px-2 py-1"
                >
                  <option value="json">Structured (JSON)</option>
                  <option value="text">Text (as in a terminal)</option>
                </select>
              </label>
              <label className="text-muted-foreground">
                Config context
                <input
                  value={context}
                  onChange={(e) => setContext(e.target.value)}
                  placeholder="(active)"
                  aria-label="Config context"
                  className="mt-1 block w-40 rounded-md border border-border bg-transparent px-2 py-1"
                />
              </label>
            </div>
          </details>

          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded-md border border-border px-2 py-1.5 text-[11px]" aria-label="Equivalent command">
              {line}
            </code>
            <button type="button" onClick={copy} className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-muted">
              <Copy className="size-3" aria-hidden="true" /> {copied ? 'Copied' : 'Copy'}
            </button>
          </div>

          {needsConfirm && permitted && (
            <label className="block rounded-lg border border-border p-3 text-xs">
              <span className="mb-1 flex items-center gap-1 font-medium">
                <ShieldAlert className="size-3.5" style={{ color: 'var(--error-text)' }} aria-hidden="true" />
                Destructive — type <code>{command.path}</code> to confirm
              </span>
              <input
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                aria-label="Type the command to confirm"
                className="mt-1 block w-full rounded-md border border-border bg-transparent px-2 py-1 font-mono"
              />
            </label>
          )}

          <div className="flex flex-wrap items-center gap-3">
            <button
              type="submit"
              disabled={!!blockedReason || start.isPending}
              title={blockedReason ?? undefined}
              className="inline-flex items-center gap-1.5 rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50"
            >
              <Play className="size-3" aria-hidden="true" /> {runLabel}
            </button>
            {blockedReason && <span className="text-[11px] text-muted-foreground">{blockedReason}</span>}
          </div>
          {msg && (
            <p role="alert" className="rounded-lg px-3 py-2 text-xs" style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-2)' }}>
              {msg}
            </p>
          )}
        </form>
      )}

      {!compact && command.examples.length > 0 && (
        <div className="space-y-1">
          <h3 className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">Examples</h3>
          <ul className="space-y-0.5 text-[11px]">
            {command.examples.map((ex) => (
              <li key={ex.cmd}>
                <code>{ex.cmd}</code>
                {ex.comment && <span className="text-muted-foreground"> — {ex.comment}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

function longOpt(p: CliParam): string {
  return p.opts?.find((o) => o.startsWith('--')) ?? p.opts?.[0] ?? p.name
}

function ParamField({
  param: p,
  value,
  onChange,
  locked = false,
}: {
  param: CliParam
  value: FormValues[string]
  onChange: (v: FormValues[string]) => void
  locked?: boolean
}) {
  const id = `cli-param-${p.name}`
  const label = p.kind === 'argument' ? p.name.toUpperCase() : longOpt(p)
  const many = p.multiple || (p.nargs ?? 1) !== 1
  const hints = [
    p.required && 'required',
    p.path && 'path inside the CLI workspace — admin',
    p.network && 'reaches another host — admin',
    p.persisting && 'persists — admin',
    many && (p.kind === 'argument' ? 'space-separated' : 'one per line'),
  ].filter(Boolean)
  const input = 'mt-1 block w-full rounded-md border border-border bg-transparent px-2 py-1 text-xs'

  if (p.flag) {
    return (
      <label htmlFor={id} className="flex items-start gap-2 text-xs sm:col-span-1">
        <input id={id} type="checkbox" checked={value === true} disabled={locked} onChange={(e) => onChange(e.target.checked)} className="mt-0.5" />
        <span>
          <code>{label}</code>
          {p.help && <span className="block text-muted-foreground">{p.help}</span>}
          {p.persisting && <span className="block text-[10px] text-muted-foreground">persists — admin</span>}
        </span>
      </label>
    )
  }

  let control
  if (p.type === 'choice') {
    control = (
      <select id={id} value={(value as string) ?? ''} disabled={locked} onChange={(e) => onChange(e.target.value)} className={input}>
        {!p.required && <option value="">(default{p.default != null ? `: ${String(p.default)}` : ''})</option>}
        {p.required && !value && <option value="">Choose…</option>}
        {p.choices?.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
    )
  } else if (many) {
    control = (
      <textarea
        id={id}
        value={Array.isArray(value) ? value.join('\n') : ((value as string) ?? '')}
        onChange={(e) => onChange(e.target.value)}
        rows={2}
        readOnly={locked}
        className={`${input} font-mono`}
      />
    )
  } else {
    control = (
      <input
        id={id}
        type={p.secret ? 'password' : p.type === 'int' || p.type === 'float' ? 'number' : 'text'}
        step={p.type === 'float' ? 'any' : undefined}
        min={p.min}
        max={p.max}
        value={(value as string) ?? ''}
        onChange={(e) => onChange(e.target.value)}
        placeholder={p.default != null && p.default !== '' ? String(p.default) : undefined}
        list={p.path ? 'cli-workspace-files' : undefined}
        readOnly={locked}
        aria-readonly={locked || undefined}
        autoComplete="off"
        className={`${input} ${p.path ? 'font-mono' : ''}`}
      />
    )
  }

  return (
    <div className="text-xs">
      <label htmlFor={id} className="font-medium">
        <code>{label}</code>
        {p.required && <span aria-hidden="true"> *</span>}
      </label>
      {control}
      {locked && <p className="mt-0.5 text-[10px] text-muted-foreground">from the selected row</p>}
      {p.help && <p className="mt-0.5 text-[11px] text-muted-foreground">{p.help}</p>}
      {hints.length > 0 && <p className="text-[10px] text-muted-foreground">{hints.join(' · ')}</p>}
    </div>
  )
}
